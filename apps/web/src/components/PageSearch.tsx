import { Input } from '@fluentui/react-components'
import { SearchRegular } from '@fluentui/react-icons'
import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

export interface SearchDestination { label: string; to: string }

export function PageSearch({ destinations }: { destinations: SearchDestination[] }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(false)
  const resultsRef = useRef<HTMLDivElement>(null)
  const matches = query.trim() ? destinations.filter((item) => item.label.toLowerCase().includes(query.trim().toLowerCase())) : []
  function close() { setQuery(''); setExpanded(false) }
  return (
    <form className="global-search" role="search"
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setExpanded(false) }}
      onSubmit={(event) => { event.preventDefault(); if (matches[0]) { navigate(matches[0].to); close() } }}>
      <Input value={query} contentBefore={<SearchRegular />} placeholder="Search pages and sections"
        aria-label="Search MOSAIC pages" aria-expanded={expanded && Boolean(query.trim())} aria-controls="page-search-results"
        onFocus={() => setExpanded(true)} onChange={(_, data) => { setQuery(data.value); setExpanded(true) }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') setExpanded(false)
          if (event.key === 'ArrowDown') { event.preventDefault(); resultsRef.current?.querySelector('a')?.focus() }
        }} />
      {expanded && query.trim() && (
        <div id="page-search-results" className="search-results" ref={resultsRef}>
          {matches.length ? matches.map((item) => <Link key={item.to} to={item.to} onClick={close}>{item.label}</Link>) :
            <p role="status">No pages or sections match “{query.trim()}”.</p>}
        </div>
      )}
    </form>
  )
}
