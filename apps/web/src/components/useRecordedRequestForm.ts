import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from '../api'
import type { AccessRequest } from '../types'

export function useRecordedRequestForm(request: AccessRequest) {
  const api = useMosaicApi()
  const form = request.recordedForm ?? request.requestForm
  const hasForm = Boolean(form || request.requestFormSummary)
  const detail = useQuery({
    queryKey: ['access-request-detail', request.id],
    queryFn: () => api.getAccessRequest(request.id),
    enabled: hasForm && !form,
    retry: false,
  })
  return { hasForm, form: form ?? detail.data?.recordedForm, detail }
}
