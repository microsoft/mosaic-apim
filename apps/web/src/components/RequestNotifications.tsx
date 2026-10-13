import { Badge, Button, Menu, MenuItem, MenuList, MenuPopover, MenuTrigger } from '@fluentui/react-components'
import { AlertRegular } from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'

export function RequestNotifications() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const requests = useQuery({
    queryKey: ['access-requests', 'pending'],
    queryFn: () => api.listAccessRequests('pending'),
    throwOnError: false,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  })
  const openQueue = () => navigate('/entitlements?tab=requests')
  return (
    <Menu>
      <MenuTrigger disableButtonEnhancement>
        <Button appearance="subtle" icon={<AlertRegular />} aria-label="Notifications">
          {requests.isSuccess && requests.data.length > 0 && <Badge color="important" appearance="filled">{requests.data.length}</Badge>}
        </Button>
      </MenuTrigger>
      <MenuPopover>
        <MenuList>
          {requests.isPending && <MenuItem disabled>Checking pending requests…</MenuItem>}
          {requests.isError && <MenuItem onClick={() => void requests.refetch()}>Requests unavailable — retry</MenuItem>}
          {requests.isSuccess && requests.data.length === 0 && <MenuItem disabled>No pending access requests</MenuItem>}
          {requests.isSuccess && requests.data.slice(0, 5).map((request) => (
            <MenuItem key={request.id} onClick={openQueue}>
              Access requested: {request.resourceSummary?.displayName ?? request.resourceSnapshot?.displayName ?? 'Resource details unavailable'}
            </MenuItem>
          ))}
          <MenuItem onClick={openQueue}>Review access requests</MenuItem>
        </MenuList>
      </MenuPopover>
    </Menu>
  )
}
