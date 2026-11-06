"""Pure-Python Apache Parquet (PAR1) reader and writer for AWS CUR / CUR 2.0 exports."""

from __future__ import annotations

import gzip
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from mosaic_api.errors import ValidationError

PARQUET_MAGIC = b"PAR1"

# Parquet physical Type enum
TYPE_BOOLEAN = 0
TYPE_INT32 = 1
TYPE_INT64 = 2
TYPE_INT96 = 3
TYPE_FLOAT = 4
TYPE_DOUBLE = 5
TYPE_BYTE_ARRAY = 6
TYPE_FIXED_LEN_BYTE_ARRAY = 7

# FieldRepetitionType enum
REP_REQUIRED = 0
REP_OPTIONAL = 1
REP_REPEATED = 2

# ConvertedType enum
CONVERTED_UTF8 = 0
CONVERTED_DECIMAL = 5

# Encoding enum
ENC_PLAIN = 0
ENC_PLAIN_DICTIONARY = 2
ENC_RLE = 3
ENC_BIT_PACKED = 4
ENC_RLE_DICTIONARY = 8

# CompressionCodec enum
CODEC_UNCOMPRESSED = 0
CODEC_SNAPPY = 1
CODEC_GZIP = 2

# PageType enum
PAGE_DATA = 0
PAGE_INDEX = 1
PAGE_DICTIONARY = 2
PAGE_DATA_V2 = 3

# Thrift Compact Protocol wire types
_CT_STOP = 0
_CT_TRUE = 1
_CT_FALSE = 2
_CT_BYTE = 3
_CT_I16 = 4
_CT_I32 = 5
_CT_I64 = 6
_CT_DOUBLE = 7
_CT_BINARY = 8
_CT_LIST = 9
_CT_SET = 10
_CT_MAP = 11
_CT_STRUCT = 12


@dataclass
class _SchemaElement:
    type: int | None = None
    type_length: int | None = None
    repetition_type: int | None = None
    name: str = ""
    num_children: int | None = None
    converted_type: int | None = None
    scale: int | None = None
    precision: int | None = None


@dataclass
class _ColumnMetaData:
    type: int = TYPE_BYTE_ARRAY
    encodings: list[int] = field(default_factory=list)
    path_in_schema: list[str] = field(default_factory=list)
    codec: int = CODEC_UNCOMPRESSED
    num_values: int = 0
    total_uncompressed_size: int = 0
    total_compressed_size: int = 0
    data_page_offset: int = 0
    index_page_offset: int | None = None
    dictionary_page_offset: int | None = None


@dataclass
class _ColumnChunk:
    file_path: str | None = None
    file_offset: int = 0
    meta_data: _ColumnMetaData = field(default_factory=_ColumnMetaData)


@dataclass
class _RowGroup:
    columns: list[_ColumnChunk] = field(default_factory=list)
    total_byte_size: int = 0
    num_rows: int = 0


@dataclass
class _FileMetaData:
    version: int = 1
    schema: list[_SchemaElement] = field(default_factory=list)
    num_rows: int = 0
    row_groups: list[_RowGroup] = field(default_factory=list)


@dataclass
class _DataPageHeader:
    num_values: int = 0
    encoding: int = ENC_PLAIN
    definition_level_encoding: int = ENC_RLE
    repetition_level_encoding: int = ENC_RLE


@dataclass
class _DictionaryPageHeader:
    num_values: int = 0
    encoding: int = ENC_PLAIN


@dataclass
class _DataPageHeaderV2:
    num_values: int = 0
    num_nulls: int = 0
    num_rows: int = 0
    encoding: int = ENC_PLAIN
    definition_levels_byte_length: int = 0
    repetition_levels_byte_length: int = 0
    is_compressed: bool = True


@dataclass
class _PageHeader:
    type: int = PAGE_DATA
    uncompressed_page_size: int = 0
    compressed_page_size: int = 0
    data_page_header: _DataPageHeader | None = None
    dictionary_page_header: _DictionaryPageHeader | None = None
    data_page_header_v2: _DataPageHeaderV2 | None = None


class _CompactReader:
    """Minimal Thrift Compact Protocol reader for Parquet metadata and page headers."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def read_byte(self) -> int:
        if self.pos >= len(self.data):
            raise ValidationError("Unexpected end of Parquet metadata stream.")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_bytes(self, length: int) -> bytes:
        if length < 0 or self.pos + length > len(self.data):
            raise ValidationError("Unexpected end of Parquet binary value.")
        chunk = self.data[self.pos : self.pos + length]
        self.pos += length
        return chunk

    def read_varint(self) -> int:
        result = 0
        shift = 0
        while True:
            b = self.read_byte()
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                return result
            shift += 7
            if shift > 70:
                raise ValidationError("Malformed varint in Parquet metadata.")

    def read_zigzag(self) -> int:
        n = self.read_varint()
        return (n >> 1) ^ -(n & 1)

    def read_double(self) -> float:
        raw = self.read_bytes(8)
        return float(struct.unpack("<d", raw)[0])

    def read_binary(self) -> bytes:
        length = self.read_varint()
        return self.read_bytes(length)

    def read_string(self) -> str:
        return self.read_binary().decode("utf-8", errors="replace")

    def read_list_header(self) -> tuple[int, int]:
        b = self.read_byte()
        size = (b >> 4) & 0x0F
        elem_type = b & 0x0F
        if size == 15:
            size = self.read_varint()
        return size, elem_type

    def skip_value(self, wire_type: int) -> None:
        if wire_type in (_CT_TRUE, _CT_FALSE):
            return
        if wire_type == _CT_BYTE:
            self.read_byte()
            return
        if wire_type in (_CT_I16, _CT_I32, _CT_I64):
            self.read_varint()
            return
        if wire_type == _CT_DOUBLE:
            self.read_bytes(8)
            return
        if wire_type == _CT_BINARY:
            self.read_binary()
            return
        if wire_type in (_CT_LIST, _CT_SET):
            size, elem_type = self.read_list_header()
            for _ in range(size):
                self.skip_value(elem_type)
            return
        if wire_type == _CT_MAP:
            size = self.read_varint()
            if size == 0:
                return
            kv = self.read_byte()
            k_type = (kv >> 4) & 0x0F
            v_type = kv & 0x0F
            for _ in range(size):
                self.skip_value(k_type)
                self.skip_value(v_type)
            return
        if wire_type == _CT_STRUCT:
            self.skip_struct()
            return
        raise ValidationError(f"Unsupported Thrift wire type {wire_type} in Parquet file.")

    def skip_struct(self) -> None:
        last_id = 0
        while True:
            field_id, wire_type = self.read_field_header(last_id)
            if wire_type == _CT_STOP:
                return
            last_id = field_id
            self.skip_value(wire_type)

    def read_field_header(self, last_field_id: int) -> tuple[int, int]:
        b = self.read_byte()
        if b == _CT_STOP:
            return 0, _CT_STOP
        delta = (b >> 4) & 0x0F
        wire_type = b & 0x0F
        field_id = self.read_zigzag() if delta == 0 else last_field_id + delta
        return field_id, wire_type


class _CompactWriter:
    """Minimal Thrift Compact Protocol writer for Parquet metadata and page headers."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._field_stack: list[int] = []
        self._last_field_id = 0

    def begin_struct(self) -> None:
        self._field_stack.append(self._last_field_id)
        self._last_field_id = 0

    def end_struct(self) -> None:
        self.buf.append(_CT_STOP)
        self._last_field_id = self._field_stack.pop()

    def write_varint(self, n: int) -> None:
        while n >= 0x80:
            self.buf.append((n & 0x7F) | 0x80)
            n >>= 7
        self.buf.append(n & 0x7F)

    def write_zigzag(self, n: int, bits: int = 32) -> None:
        mask = (1 << bits) - 1
        zz = ((n << 1) ^ (n >> (bits - 1))) & mask
        self.write_varint(zz)

    def _write_field_header(self, field_id: int, wire_type: int) -> None:
        delta = field_id - self._last_field_id
        if 1 <= delta <= 15:
            self.buf.append((delta << 4) | wire_type)
        else:
            self.buf.append(wire_type)
            self.write_zigzag(field_id, 16)
        self._last_field_id = field_id

    def write_i32_field(self, field_id: int, value: int) -> None:
        self._write_field_header(field_id, _CT_I32)
        self.write_zigzag(value, 32)

    def write_i64_field(self, field_id: int, value: int) -> None:
        self._write_field_header(field_id, _CT_I64)
        self.write_zigzag(value, 64)

    def write_bool_field(self, field_id: int, value: bool) -> None:
        self._write_field_header(field_id, _CT_TRUE if value else _CT_FALSE)

    def write_string_field(self, field_id: int, value: str) -> None:
        self._write_field_header(field_id, _CT_BINARY)
        encoded = value.encode("utf-8")
        self.write_varint(len(encoded))
        self.buf.extend(encoded)

    def write_list_header(self, field_id: int, elem_type: int, size: int) -> None:
        self._write_field_header(field_id, _CT_LIST)
        if size < 15:
            self.buf.append((size << 4) | elem_type)
        else:
            self.buf.append(0xF0 | elem_type)
            self.write_varint(size)

    def write_struct_field(self, field_id: int) -> None:
        self._write_field_header(field_id, _CT_STRUCT)
        self.begin_struct()


def _read_schema_element(reader: _CompactReader) -> _SchemaElement:
    elem = _SchemaElement()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return elem
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            elem.type = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_I32:
            elem.type_length = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_I32:
            elem.repetition_type = reader.read_zigzag()
        elif field_id == 4 and wire_type == _CT_BINARY:
            elem.name = reader.read_string()
        elif field_id == 5 and wire_type == _CT_I32:
            elem.num_children = reader.read_zigzag()
        elif field_id == 6 and wire_type == _CT_I32:
            elem.converted_type = reader.read_zigzag()
        elif field_id == 7 and wire_type == _CT_I32:
            elem.scale = reader.read_zigzag()
        elif field_id == 8 and wire_type == _CT_I32:
            elem.precision = reader.read_zigzag()
        else:
            reader.skip_value(wire_type)


def _read_column_meta_data(reader: _CompactReader) -> _ColumnMetaData:
    meta = _ColumnMetaData()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return meta
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            meta.type = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_LIST:
            size, elem_type = reader.read_list_header()
            for _ in range(size):
                if elem_type == _CT_I32:
                    meta.encodings.append(reader.read_zigzag())
                else:
                    reader.skip_value(elem_type)
        elif field_id == 3 and wire_type == _CT_LIST:
            size, elem_type = reader.read_list_header()
            for _ in range(size):
                if elem_type == _CT_BINARY:
                    meta.path_in_schema.append(reader.read_string())
                else:
                    reader.skip_value(elem_type)
        elif field_id == 4 and wire_type == _CT_I32:
            meta.codec = reader.read_zigzag()
        elif field_id == 5 and wire_type == _CT_I64:
            meta.num_values = reader.read_zigzag()
        elif field_id == 6 and wire_type == _CT_I64:
            meta.total_uncompressed_size = reader.read_zigzag()
        elif field_id == 7 and wire_type == _CT_I64:
            meta.total_compressed_size = reader.read_zigzag()
        elif field_id == 9 and wire_type == _CT_I64:
            meta.data_page_offset = reader.read_zigzag()
        elif field_id == 10 and wire_type == _CT_I64:
            meta.index_page_offset = reader.read_zigzag()
        elif field_id == 11 and wire_type == _CT_I64:
            meta.dictionary_page_offset = reader.read_zigzag()
        else:
            reader.skip_value(wire_type)


def _read_column_chunk(reader: _CompactReader) -> _ColumnChunk:
    chunk = _ColumnChunk()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return chunk
        last_id = field_id
        if field_id == 1 and wire_type == _CT_BINARY:
            chunk.file_path = reader.read_string()
        elif field_id == 2 and wire_type == _CT_I64:
            chunk.file_offset = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_STRUCT:
            chunk.meta_data = _read_column_meta_data(reader)
        else:
            reader.skip_value(wire_type)


def _read_row_group(reader: _CompactReader) -> _RowGroup:
    rg = _RowGroup()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return rg
        last_id = field_id
        if field_id == 1 and wire_type == _CT_LIST:
            size, elem_type = reader.read_list_header()
            for _ in range(size):
                if elem_type == _CT_STRUCT:
                    rg.columns.append(_read_column_chunk(reader))
                else:
                    reader.skip_value(elem_type)
        elif field_id == 2 and wire_type == _CT_I64:
            rg.total_byte_size = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_I64:
            rg.num_rows = reader.read_zigzag()
        else:
            reader.skip_value(wire_type)


def _read_file_meta_data(data: bytes) -> _FileMetaData:
    reader = _CompactReader(data)
    meta = _FileMetaData()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return meta
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            meta.version = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_LIST:
            size, elem_type = reader.read_list_header()
            for _ in range(size):
                if elem_type == _CT_STRUCT:
                    meta.schema.append(_read_schema_element(reader))
                else:
                    reader.skip_value(elem_type)
        elif field_id == 3 and wire_type == _CT_I64:
            meta.num_rows = reader.read_zigzag()
        elif field_id == 4 and wire_type == _CT_LIST:
            size, elem_type = reader.read_list_header()
            for _ in range(size):
                if elem_type == _CT_STRUCT:
                    meta.row_groups.append(_read_row_group(reader))
                else:
                    reader.skip_value(elem_type)
        else:
            reader.skip_value(wire_type)


def _read_data_page_header(reader: _CompactReader) -> _DataPageHeader:
    hdr = _DataPageHeader()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return hdr
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            hdr.num_values = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_I32:
            hdr.encoding = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_I32:
            hdr.definition_level_encoding = reader.read_zigzag()
        elif field_id == 4 and wire_type == _CT_I32:
            hdr.repetition_level_encoding = reader.read_zigzag()
        else:
            reader.skip_value(wire_type)


def _read_dictionary_page_header(reader: _CompactReader) -> _DictionaryPageHeader:
    hdr = _DictionaryPageHeader()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return hdr
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            hdr.num_values = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_I32:
            hdr.encoding = reader.read_zigzag()
        else:
            reader.skip_value(wire_type)


def _read_data_page_header_v2(reader: _CompactReader) -> _DataPageHeaderV2:
    hdr = _DataPageHeaderV2()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return hdr
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            hdr.num_values = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_I32:
            hdr.num_nulls = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_I32:
            hdr.num_rows = reader.read_zigzag()
        elif field_id == 4 and wire_type == _CT_I32:
            hdr.encoding = reader.read_zigzag()
        elif field_id == 5 and wire_type == _CT_I32:
            hdr.definition_levels_byte_length = reader.read_zigzag()
        elif field_id == 6 and wire_type == _CT_I32:
            hdr.repetition_levels_byte_length = reader.read_zigzag()
        elif field_id == 7 and wire_type in (_CT_TRUE, _CT_FALSE):
            hdr.is_compressed = wire_type == _CT_TRUE
        else:
            reader.skip_value(wire_type)


def _read_page_header(data: bytes, offset: int) -> tuple[_PageHeader, int]:
    reader = _CompactReader(data, offset)
    hdr = _PageHeader()
    last_id = 0
    while True:
        field_id, wire_type = reader.read_field_header(last_id)
        if wire_type == _CT_STOP:
            return hdr, reader.pos
        last_id = field_id
        if field_id == 1 and wire_type == _CT_I32:
            hdr.type = reader.read_zigzag()
        elif field_id == 2 and wire_type == _CT_I32:
            hdr.uncompressed_page_size = reader.read_zigzag()
        elif field_id == 3 and wire_type == _CT_I32:
            hdr.compressed_page_size = reader.read_zigzag()
        elif field_id == 5 and wire_type == _CT_STRUCT:
            hdr.data_page_header = _read_data_page_header(reader)
        elif field_id == 7 and wire_type == _CT_STRUCT:
            hdr.dictionary_page_header = _read_dictionary_page_header(reader)
        elif field_id == 8 and wire_type == _CT_STRUCT:
            hdr.data_page_header_v2 = _read_data_page_header_v2(reader)
        else:
            reader.skip_value(wire_type)


def _snappy_decompress(data: bytes, max_length: int) -> bytes:
    """Pure-Python Snappy block decompressor for Parquet pages."""

    pos = 0
    n = len(data)
    target_len = 0
    shift = 0
    while pos < n:
        b = data[pos]
        pos += 1
        target_len |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
        if shift > 35:
            raise ValidationError("Invalid Snappy uncompressed length in Parquet page.")

    if target_len > max_length:
        raise ValidationError(
            f"Decompressed Parquet Snappy page ({target_len} bytes) exceeds allowed limit."
        )

    out = bytearray()
    while pos < n:
        tag = data[pos]
        pos += 1
        tag_type = tag & 0x03
        if tag_type == 0:
            # Literal
            length_code = tag >> 2
            if length_code < 60:
                lit_len = length_code + 1
            else:
                extra_bytes = length_code - 59
                if pos + extra_bytes > n:
                    raise ValidationError("Truncated Snappy literal in Parquet page.")
                lit_len = int.from_bytes(data[pos : pos + extra_bytes], "little") + 1
                pos += extra_bytes
            if pos + lit_len > n or len(out) + lit_len > target_len:
                raise ValidationError("Snappy literal overflows Parquet page buffer.")
            out.extend(data[pos : pos + lit_len])
            pos += lit_len
        else:
            if tag_type == 1:
                copy_len = ((tag >> 2) & 0x07) + 4
                if pos >= n:
                    raise ValidationError("Truncated Snappy 1-byte offset in Parquet page.")
                offset = ((tag & 0xE0) << 3) | data[pos]
                pos += 1
            elif tag_type == 2:
                copy_len = (tag >> 2) + 1
                if pos + 2 > n:
                    raise ValidationError("Truncated Snappy 2-byte offset in Parquet page.")
                offset = int.from_bytes(data[pos : pos + 2], "little")
                pos += 2
            else:
                copy_len = (tag >> 2) + 1
                if pos + 4 > n:
                    raise ValidationError("Truncated Snappy 4-byte offset in Parquet page.")
                offset = int.from_bytes(data[pos : pos + 4], "little")
                pos += 4
            if offset <= 0 or offset > len(out) or len(out) + copy_len > target_len:
                raise ValidationError("Invalid Snappy copy offset in Parquet page.")
            if offset >= copy_len:
                start = len(out) - offset
                out.extend(out[start : start + copy_len])
            else:
                for _ in range(copy_len):
                    out.append(out[len(out) - offset])

    if len(out) != target_len:
        raise ValidationError("Snappy decompressed length mismatch in Parquet page.")
    return bytes(out)


def _decompress_page(raw: bytes, codec: int, expected_size: int, max_bytes: int) -> bytes:
    if expected_size > max_bytes:
        raise ValidationError("Parquet page uncompressed size exceeds safety limit.")
    if codec == CODEC_UNCOMPRESSED:
        return raw
    if codec == CODEC_GZIP:
        try:
            out = gzip.decompress(raw)
        except Exception as exc:
            raise ValidationError(f"Failed to decompress GZIP Parquet page: {exc}") from exc
        if len(out) > max_bytes:
            raise ValidationError("Decompressed GZIP Parquet page exceeds safety limit.")
        return out
    if codec == CODEC_SNAPPY:
        return _snappy_decompress(raw, max_bytes)
    raise ValidationError(
        f"Unsupported Parquet compression codec ({codec}); use UNCOMPRESSED, SNAPPY, or GZIP."
    )


def _decode_rle_bit_packed_hybrid(
    buf: bytes,
    pos: int,
    bit_width: int,
    count: int,
    *,
    end_pos: int | None = None,
) -> tuple[list[int], int]:
    """Decode ``count`` integers from a Parquet RLE / Bit-Packing Hybrid stream."""

    if count == 0:
        return [], pos
    if bit_width == 0:
        return [0] * count, pos

    limit = len(buf) if end_pos is None else end_pos
    values: list[int] = []
    byte_width = (bit_width + 7) // 8
    mask = (1 << bit_width) - 1

    while len(values) < count and pos < limit:
        # Read unsigned varint header
        header = 0
        shift = 0
        while True:
            if pos >= limit:
                raise ValidationError("Truncated RLE header in Parquet page.")
            b = buf[pos]
            pos += 1
            header |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7

        if (header & 1) == 0:
            # RLE run
            run_len = header >> 1
            if pos + byte_width > limit:
                raise ValidationError("Truncated RLE value in Parquet page.")
            val = int.from_bytes(buf[pos : pos + byte_width], "little") & mask
            pos += byte_width
            take = min(run_len, count - len(values))
            values.extend([val] * take)
        else:
            # Bit-packed run: groups of 8 values
            num_groups = header >> 1
            num_vals = num_groups * 8
            total_bytes = num_groups * bit_width
            if pos + total_bytes > limit:
                raise ValidationError("Truncated bit-packed run in Parquet page.")
            chunk = buf[pos : pos + total_bytes]
            pos += total_bytes
            bit_stream = int.from_bytes(chunk, "little")
            take = min(num_vals, count - len(values))
            for i in range(take):
                values.append((bit_stream >> (i * bit_width)) & mask)

    if len(values) < count:
        raise ValidationError("Parquet RLE stream ended before all values were decoded.")
    return values, pos


def _encode_rle_run(values: Sequence[int], bit_width: int) -> bytes:
    """Encode integers using Parquet RLE runs (with 4-byte length prefix helper available)."""

    if not values or bit_width == 0:
        return b""
    out = bytearray()
    byte_width = (bit_width + 7) // 8
    i = 0
    n = len(values)
    while i < n:
        val = values[i]
        run_len = 1
        while i + run_len < n and values[i + run_len] == val:
            run_len += 1
        header = run_len << 1  # LSB = 0 for RLE
        while header >= 0x80:
            out.append((header & 0x7F) | 0x80)
            header >>= 7
        out.append(header & 0x7F)
        out.extend(int(val).to_bytes(byte_width, "little", signed=False))
        i += run_len
    return bytes(out)


def _decode_plain_values(
    buf: bytes,
    pos: int,
    count: int,
    phys_type: int,
    schema_elem: _SchemaElement,
) -> tuple[list[Any], int]:
    values: list[Any] = []
    scale = schema_elem.scale or 0
    is_decimal = schema_elem.converted_type == CONVERTED_DECIMAL

    if phys_type == TYPE_BOOLEAN:
        byte_count = (count + 7) // 8
        if pos + byte_count > len(buf):
            raise ValidationError("Truncated BOOLEAN PLAIN values in Parquet page.")
        for i in range(count):
            b = buf[pos + (i // 8)]
            values.append(bool((b >> (i % 8)) & 1))
        return values, pos + byte_count

    if phys_type == TYPE_INT32:
        need = count * 4
        if pos + need > len(buf):
            raise ValidationError("Truncated INT32 PLAIN values in Parquet page.")
        for i in range(count):
            raw_i32 = int(struct.unpack_from("<i", buf, pos + i * 4)[0])
            if is_decimal:
                values.append(Decimal(raw_i32).scaleb(-scale))
            else:
                values.append(raw_i32)
        return values, pos + need

    if phys_type == TYPE_INT64:
        need = count * 8
        if pos + need > len(buf):
            raise ValidationError("Truncated INT64 PLAIN values in Parquet page.")
        for i in range(count):
            raw_i64 = int(struct.unpack_from("<q", buf, pos + i * 8)[0])
            if is_decimal:
                values.append(Decimal(raw_i64).scaleb(-scale))
            else:
                values.append(raw_i64)
        return values, pos + need

    if phys_type == TYPE_FLOAT:
        need = count * 4
        if pos + need > len(buf):
            raise ValidationError("Truncated FLOAT PLAIN values in Parquet page.")
        for i in range(count):
            values.append(float(struct.unpack_from("<f", buf, pos + i * 4)[0]))
        return values, pos + need

    if phys_type == TYPE_DOUBLE:
        need = count * 8
        if pos + need > len(buf):
            raise ValidationError("Truncated DOUBLE PLAIN values in Parquet page.")
        for i in range(count):
            values.append(float(struct.unpack_from("<d", buf, pos + i * 8)[0]))
        return values, pos + need

    if phys_type == TYPE_BYTE_ARRAY:
        for _ in range(count):
            if pos + 4 > len(buf):
                raise ValidationError("Truncated BYTE_ARRAY length in Parquet page.")
            length = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
            if length < 0 or pos + length > len(buf):
                raise ValidationError("Truncated BYTE_ARRAY payload in Parquet page.")
            raw_bytes = buf[pos : pos + length]
            pos += length
            if is_decimal:
                unscaled = int.from_bytes(raw_bytes, "big", signed=True)
                values.append(Decimal(unscaled).scaleb(-scale))
            else:
                values.append(raw_bytes.decode("utf-8", errors="replace"))
        return values, pos

    if phys_type == TYPE_FIXED_LEN_BYTE_ARRAY:
        fixed_len = schema_elem.type_length or 0
        if fixed_len <= 0:
            raise ValidationError("Invalid FIXED_LEN_BYTE_ARRAY length in Parquet schema.")
        need = count * fixed_len
        if pos + need > len(buf):
            raise ValidationError("Truncated FIXED_LEN_BYTE_ARRAY values in Parquet page.")
        for i in range(count):
            raw_bytes = buf[pos + i * fixed_len : pos + (i + 1) * fixed_len]
            if is_decimal:
                unscaled = int.from_bytes(raw_bytes, "big", signed=True)
                values.append(Decimal(unscaled).scaleb(-scale))
            else:
                values.append(raw_bytes.decode("utf-8", errors="replace"))
        return values, pos + need

    raise ValidationError(f"Unsupported Parquet physical type: {phys_type}")


def _read_column_values(
    data: bytes,
    chunk: _ColumnChunk,
    schema_elem: _SchemaElement,
    *,
    max_decompressed_bytes: int,
) -> tuple[list[Any], int]:
    meta = chunk.meta_data
    total_expected = meta.num_values
    if total_expected == 0:
        return [], 0

    offset = meta.data_page_offset
    if (
        meta.dictionary_page_offset is not None
        and meta.dictionary_page_offset > 0
        and (offset == 0 or meta.dictionary_page_offset < offset)
    ):
        offset = meta.dictionary_page_offset
    elif offset == 0 and chunk.file_offset > 0:
        offset = chunk.file_offset

    repetition_type = schema_elem.repetition_type
    if repetition_type is None:
        repetition_type = REP_OPTIONAL
    is_optional = repetition_type != REP_REQUIRED
    dictionary: list[Any] | None = None
    col_values: list[Any] = []
    decompressed_total = 0

    while len(col_values) < total_expected:
        if offset >= len(data):
            raise ValidationError("Unexpected EOF while reading Parquet column pages.")
        page_hdr, payload_start = _read_page_header(data, offset)
        comp_size = page_hdr.compressed_page_size
        uncomp_size = page_hdr.uncompressed_page_size
        if comp_size < 0 or payload_start + comp_size > len(data):
            raise ValidationError("Parquet page extends beyond file boundary.")
        raw_page = data[payload_start : payload_start + comp_size]
        offset = payload_start + comp_size

        if page_hdr.type == PAGE_INDEX:
            continue

        decompressed_total += max(uncomp_size, len(raw_page))
        if decompressed_total > max_decompressed_bytes:
            raise ValidationError("Decompressed Parquet column exceeds maximum allowed size.")

        if page_hdr.type == PAGE_DICTIONARY:
            dict_hdr = page_hdr.dictionary_page_header
            if dict_hdr is None:
                raise ValidationError("Parquet DICTIONARY_PAGE missing dictionary_page_header.")
            page_bytes = _decompress_page(raw_page, meta.codec, uncomp_size, max_decompressed_bytes)
            dictionary, _ = _decode_plain_values(
                page_bytes, 0, dict_hdr.num_values, meta.type, schema_elem
            )
            continue

        if page_hdr.type == PAGE_DATA:
            dp_hdr = page_hdr.data_page_header
            if dp_hdr is None:
                raise ValidationError("Parquet DATA_PAGE missing data_page_header.")
            page_bytes = _decompress_page(raw_page, meta.codec, uncomp_size, max_decompressed_bytes)
            pos = 0
            num_page_vals = dp_hdr.num_values
            if is_optional:
                if dp_hdr.definition_level_encoding != ENC_RLE:
                    raise ValidationError("Unsupported Parquet definition level encoding.")
                if pos + 4 > len(page_bytes):
                    raise ValidationError("Truncated definition level length in Parquet DATA_PAGE.")
                def_byte_len = int.from_bytes(page_bytes[pos : pos + 4], "little")
                pos += 4
                def_levels, _ = _decode_rle_bit_packed_hybrid(
                    page_bytes,
                    pos,
                    1,
                    num_page_vals,
                    end_pos=pos + def_byte_len,
                )
                pos += def_byte_len
            else:
                def_levels = [1] * num_page_vals

            non_null_count = sum(1 for dl in def_levels if dl > 0)
            enc = dp_hdr.encoding
            if enc == ENC_PLAIN:
                non_null_vals, pos = _decode_plain_values(
                    page_bytes, pos, non_null_count, meta.type, schema_elem
                )
            elif enc in (ENC_PLAIN_DICTIONARY, ENC_RLE_DICTIONARY):
                if dictionary is None:
                    raise ValidationError(
                        "Parquet dictionary-encoded DATA_PAGE has no preceding DICTIONARY_PAGE."
                    )
                if non_null_count == 0:
                    non_null_vals = []
                else:
                    if pos >= len(page_bytes):
                        raise ValidationError(
                            "Truncated dictionary bit width in Parquet DATA_PAGE."
                        )
                    bit_width = page_bytes[pos]
                    pos += 1
                    indices, pos = _decode_rle_bit_packed_hybrid(
                        page_bytes, pos, bit_width, non_null_count
                    )
                    try:
                        non_null_vals = [dictionary[idx] for idx in indices]
                    except IndexError as exc:
                        raise ValidationError(
                            "Parquet dictionary index out of bounds."
                        ) from exc
            else:
                raise ValidationError(f"Unsupported Parquet page encoding: {enc}")

            val_iter = iter(non_null_vals)
            for dl in def_levels:
                col_values.append(next(val_iter) if dl > 0 else None)
            continue

        if page_hdr.type == PAGE_DATA_V2:
            dp2_hdr = page_hdr.data_page_header_v2
            if dp2_hdr is None:
                raise ValidationError("Parquet DATA_PAGE_V2 missing data_page_header_v2.")
            num_page_vals = dp2_hdr.num_values
            rep_len = dp2_hdr.repetition_levels_byte_length
            def_len = dp2_hdr.definition_levels_byte_length
            if rep_len + def_len > len(raw_page):
                raise ValidationError("Invalid level lengths in Parquet DATA_PAGE_V2.")
            def_bytes = raw_page[rep_len : rep_len + def_len]
            compressed_data = raw_page[rep_len + def_len :]
            if is_optional and def_len > 0:
                def_levels, _ = _decode_rle_bit_packed_hybrid(
                    def_bytes, 0, 1, num_page_vals, end_pos=def_len
                )
            else:
                def_levels = [1] * num_page_vals

            data_bytes = (
                _decompress_page(
                    compressed_data,
                    meta.codec,
                    max(uncomp_size - rep_len - def_len, 0),
                    max_decompressed_bytes,
                )
                if dp2_hdr.is_compressed
                else compressed_data
            )
            non_null_count = sum(1 for dl in def_levels if dl > 0)
            enc = dp2_hdr.encoding
            if enc == ENC_PLAIN:
                non_null_vals, _ = _decode_plain_values(
                    data_bytes, 0, non_null_count, meta.type, schema_elem
                )
            elif enc in (ENC_PLAIN_DICTIONARY, ENC_RLE_DICTIONARY):
                if dictionary is None:
                    raise ValidationError(
                        "Parquet dictionary-encoded DATA_PAGE_V2 has no DICTIONARY_PAGE."
                    )
                if non_null_count == 0:
                    non_null_vals = []
                else:
                    bit_width = data_bytes[0]
                    indices, _ = _decode_rle_bit_packed_hybrid(
                        data_bytes, 1, bit_width, non_null_count
                    )
                    non_null_vals = [dictionary[idx] for idx in indices]
            else:
                raise ValidationError(f"Unsupported Parquet DATA_PAGE_V2 encoding: {enc}")

            val_iter = iter(non_null_vals)
            for dl in def_levels:
                col_values.append(next(val_iter) if dl > 0 else None)
            continue

        raise ValidationError(f"Unsupported Parquet page type: {page_hdr.type}")

    return col_values[:total_expected], decompressed_total


def read_parquet_table(
    data: bytes,
    *,
    max_rows: int = 100_000,
    max_decompressed_bytes: int = 50 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Read a Parquet (PAR1) binary payload into a list of row dicts."""

    if len(data) < 12 or not data.startswith(PARQUET_MAGIC) or not data.endswith(PARQUET_MAGIC):
        raise ValidationError(
            "Invalid Parquet file: missing PAR1 magic header or footer."
        )

    footer_len = int.from_bytes(data[-8:-4], "little")
    footer_start = len(data) - 8 - footer_len
    if footer_len <= 0 or footer_start < 4:
        raise ValidationError("Invalid Parquet file: corrupted footer length.")

    file_meta = _read_file_meta_data(data[footer_start : len(data) - 8])
    if file_meta.num_rows > max_rows:
        raise ValidationError(
            f"Parquet billing dataset has {file_meta.num_rows:,} rows, which exceeds the "
            f"maximum allowed row count ({max_rows:,} rows)."
        )

    # Leaf schema elements are those with a physical type set (non-root/non-group)
    leaf_elements = [elem for elem in file_meta.schema if elem.type is not None]
    leaf_by_name = {elem.name: elem for elem in leaf_elements}

    rows: list[dict[str, Any]] = []
    total_decompressed = 0

    for rg in file_meta.row_groups:
        if len(rows) + rg.num_rows > max_rows:
            raise ValidationError(
                f"Parquet billing dataset exceeds the maximum allowed row count "
                f"({max_rows:,} rows)."
            )
        col_data: dict[str, list[Any]] = {}
        for idx, col_chunk in enumerate(rg.columns):
            path_name = (
                col_chunk.meta_data.path_in_schema[-1]
                if col_chunk.meta_data.path_in_schema
                else (leaf_elements[idx].name if idx < len(leaf_elements) else f"col_{idx}")
            )
            schema_elem = leaf_by_name.get(path_name) or (
                leaf_elements[idx]
                if idx < len(leaf_elements)
                else _SchemaElement(
                    type=col_chunk.meta_data.type,
                    repetition_type=REP_OPTIONAL,
                    name=path_name,
                )
            )
            remaining_budget = max_decompressed_bytes - total_decompressed
            if remaining_budget <= 0:
                raise ValidationError(
                    "Decompressed Parquet dataset exceeds the maximum allowed size."
                )
            values, used_bytes = _read_column_values(
                data,
                col_chunk,
                schema_elem,
                max_decompressed_bytes=remaining_budget,
            )
            total_decompressed += used_bytes
            col_data[path_name] = values

        for row_idx in range(rg.num_rows):
            row: dict[str, Any] = {}
            for col_name, values in col_data.items():
                row[col_name] = values[row_idx] if row_idx < len(values) else None
            rows.append(row)

    return rows


def _encode_plain_values(values: Sequence[Any], phys_type: int) -> bytes:
    out = bytearray()
    if phys_type == TYPE_DOUBLE:
        for val in values:
            out.extend(struct.pack("<d", float(val)))
        return bytes(out)
    if phys_type == TYPE_INT64:
        for val in values:
            out.extend(struct.pack("<q", int(val)))
        return bytes(out)
    if phys_type == TYPE_BOOLEAN:
        byte_len = (len(values) + 7) // 8
        packed = bytearray(byte_len)
        for i, val in enumerate(values):
            if val:
                packed[i // 8] |= 1 << (i % 8)
        return bytes(packed)
    # Default: TYPE_BYTE_ARRAY (UTF-8 strings)
    for val in values:
        encoded = str(val).encode("utf-8")
        out.extend(len(encoded).to_bytes(4, "little"))
        out.extend(encoded)
    return bytes(out)


def _write_page_header_bytes(
    page_type: int,
    uncompressed_size: int,
    compressed_size: int,
    *,
    num_values: int,
    encoding: int,
    is_dictionary: bool = False,
) -> bytes:
    w = _CompactWriter()
    w.begin_struct()
    w.write_i32_field(1, page_type)
    w.write_i32_field(2, uncompressed_size)
    w.write_i32_field(3, compressed_size)
    if is_dictionary:
        w.write_struct_field(7)
        w.write_i32_field(1, num_values)
        w.write_i32_field(2, encoding)
        w.end_struct()
    else:
        w.write_struct_field(5)
        w.write_i32_field(1, num_values)
        w.write_i32_field(2, encoding)
        w.write_i32_field(3, ENC_RLE)
        w.write_i32_field(4, ENC_RLE)
        w.end_struct()
    w.end_struct()
    return bytes(w.buf)


def write_parquet_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    compression: Literal["UNCOMPRESSED", "GZIP"] = "UNCOMPRESSED",
    use_dictionary: bool = False,
) -> bytes:
    """Serialize a sequence of row dicts into a valid Apache Parquet (PAR1) binary payload."""

    columns: list[str] = []
    seen_cols: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen_cols:
                seen_cols.add(key)
                columns.append(key)

    codec = CODEC_GZIP if compression == "GZIP" else CODEC_UNCOMPRESSED
    out = bytearray(PARQUET_MAGIC)

    schema_elements: list[_SchemaElement] = [
        _SchemaElement(name="schema", num_children=len(columns))
    ]
    col_chunks: list[_ColumnChunk] = []
    total_rg_bytes = 0

    for col_name in columns:
        col_vals = [row.get(col_name) for row in rows]
        non_nulls = [v for v in col_vals if v is not None]

        # Infer physical type
        if non_nulls and all(isinstance(v, bool) for v in non_nulls):
            phys_type = TYPE_BOOLEAN
            conv_type = None
        elif non_nulls and all(isinstance(v, int) and not isinstance(v, bool) for v in non_nulls):
            phys_type = TYPE_INT64
            conv_type = None
        elif non_nulls and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_nulls
        ):
            phys_type = TYPE_DOUBLE
            conv_type = None
        else:
            phys_type = TYPE_BYTE_ARRAY
            conv_type = CONVERTED_UTF8

        schema_elements.append(
            _SchemaElement(
                type=phys_type,
                repetition_type=REP_OPTIONAL,
                name=col_name,
                converted_type=conv_type,
            )
        )

        def_levels = [0 if v is None else 1 for v in col_vals]
        rle_def = _encode_rle_run(def_levels, 1)
        def_prefix = len(rle_def).to_bytes(4, "little") + rle_def

        dict_page_offset: int | None = None
        col_start_offset = len(out)
        encodings_used = [ENC_RLE]

        if use_dictionary and non_nulls and phys_type == TYPE_BYTE_ARRAY:
            dict_Page_offset_val = len(out)
            dict_page_offset = dict_Page_offset_val
            unique_vals: list[str] = []
            val_to_idx: dict[str, int] = {}
            indices: list[int] = []
            for v in non_nulls:
                s = str(v)
                if s not in val_to_idx:
                    val_to_idx[s] = len(unique_vals)
                    unique_vals.append(s)
                indices.append(val_to_idx[s])

            dict_plain = _encode_plain_values(unique_vals, TYPE_BYTE_ARRAY)
            dict_comp = gzip.compress(dict_plain) if codec == CODEC_GZIP else dict_plain
            dict_hdr = _write_page_header_bytes(
                PAGE_DICTIONARY,
                len(dict_plain),
                len(dict_comp),
                num_values=len(unique_vals),
                encoding=ENC_PLAIN,
                is_dictionary=True,
            )
            out.extend(dict_hdr)
            out.extend(dict_comp)

            bit_width = max(1, math.ceil(math.log2(len(unique_vals) + 1)))
            rle_indices = bytes([bit_width]) + _encode_rle_run(indices, bit_width)
            data_uncomp = def_prefix + rle_indices
            page_encoding = ENC_RLE_DICTIONARY
            encodings_used.extend([ENC_PLAIN, ENC_RLE_DICTIONARY])
        else:
            plain_payload = _encode_plain_values(non_nulls, phys_type)
            data_uncomp = def_prefix + plain_payload
            page_encoding = ENC_PLAIN
            encodings_used.append(ENC_PLAIN)

        data_page_offset = len(out)
        data_comp = gzip.compress(data_uncomp) if codec == CODEC_GZIP else data_uncomp
        data_hdr = _write_page_header_bytes(
            PAGE_DATA,
            len(data_uncomp),
            len(data_comp),
            num_values=len(col_vals),
            encoding=page_encoding,
            is_dictionary=False,
        )
        out.extend(data_hdr)
        out.extend(data_comp)

        col_total_comp = len(out) - col_start_offset
        col_total_uncomp = len(data_uncomp)
        total_rg_bytes += col_total_uncomp

        col_chunks.append(
            _ColumnChunk(
                file_offset=col_start_offset,
                meta_data=_ColumnMetaData(
                    type=phys_type,
                    encodings=encodings_used,
                    path_in_schema=[col_name],
                    codec=codec,
                    num_values=len(col_vals),
                    total_uncompressed_size=col_total_uncomp,
                    total_compressed_size=col_total_comp,
                    data_page_offset=data_page_offset,
                    dictionary_page_offset=dict_page_offset,
                ),
            )
        )

    # Write FileMetaData footer
    fw = _CompactWriter()
    fw.begin_struct()
    fw.write_i32_field(1, 1)  # version = 1
    fw.write_list_header(2, _CT_STRUCT, len(schema_elements))
    for se in schema_elements:
        fw.begin_struct()
        if se.type is not None:
            fw.write_i32_field(1, se.type)
        if se.repetition_type is not None:
            fw.write_i32_field(3, se.repetition_type)
        fw.write_string_field(4, se.name)
        if se.num_children is not None:
            fw.write_i32_field(5, se.num_children)
        if se.converted_type is not None:
            fw.write_i32_field(6, se.converted_type)
        fw.end_struct()
    fw.write_i64_field(3, len(rows))  # num_rows
    fw.write_list_header(4, _CT_STRUCT, 1)  # 1 row group
    fw.begin_struct()
    fw.write_list_header(1, _CT_STRUCT, len(col_chunks))
    for cc in col_chunks:
        fw.begin_struct()
        fw.write_i64_field(2, cc.file_offset)
        fw.write_struct_field(3)  # meta_data
        md = cc.meta_data
        fw.write_i32_field(1, md.type)
        fw.write_list_header(2, _CT_I32, len(md.encodings))
        for enc in md.encodings:
            fw.write_zigzag(enc, 32)
        fw.write_list_header(3, _CT_BINARY, len(md.path_in_schema))
        for p in md.path_in_schema:
            b_p = p.encode("utf-8")
            fw.write_varint(len(b_p))
            fw.buf.extend(b_p)
        fw.write_i32_field(4, md.codec)
        fw.write_i64_field(5, md.num_values)
        fw.write_i64_field(6, md.total_uncompressed_size)
        fw.write_i64_field(7, md.total_compressed_size)
        fw.write_i64_field(9, md.data_page_offset)
        if md.dictionary_page_offset is not None:
            fw.write_i64_field(11, md.dictionary_page_offset)
        fw.end_struct()  # end meta_data
        fw.end_struct()  # end ColumnChunk
    fw.write_i64_field(2, total_rg_bytes)
    fw.write_i64_field(3, len(rows))
    fw.end_struct()  # end RowGroup
    fw.end_struct()  # end FileMetaData

    footer_bytes = bytes(fw.buf)
    out.extend(footer_bytes)
    out.extend(len(footer_bytes).to_bytes(4, "little"))
    out.extend(PARQUET_MAGIC)
    return bytes(out)
