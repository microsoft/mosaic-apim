import {
  Button,
  Input,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Text,
  Title3,
  Tooltip,
} from '@fluentui/react-components'
import {
  AddRegular,
  AlertRegular,
  ChartMultipleRegular,
  CloudDatabaseRegular,
  CodeTextEditRegular,
  DismissRegular,
  HomeRegular,
  HistoryRegular,
  NavigationRegular,
  PersonAccountsRegular,
  PersonCircleRegular,
  PlugConnectedRegular,
  QuestionCircleRegular,
  SearchRegular,
  SettingsRegular,
  ShieldKeyholeRegular,
  SignOutRegular,
} from '@fluentui/react-icons'
import { useMsal } from '@azure/msal-react'
import { type FormEvent, type ReactNode, useMemo, useState } from 'react'
import {
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
import { AuthGate } from './auth'
import { AdminProfilePage } from './pages/AdminProfilePage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { DashboardPage } from './pages/DashboardPage'
import { EntitlementsPage } from './pages/EntitlementsPage'
import { GatewayDetailPage } from './pages/GatewayDetailPage'
import { GatewaysPage } from './pages/GatewaysPage'
import { IdentityPage } from './pages/IdentityPage'
import { ModelFoundryPage } from './pages/ModelFoundryPage'
import { PoliciesPage } from './pages/PoliciesPage'
import { SettingsPage } from './pages/SettingsPage'
import { SupportPage } from './pages/SupportPage'
import { runtimeConfig } from './runtime-config'
import { MosaicThemeProvider } from './theme'
import './index.css'

interface NavigationItem {
  to: string
  label: string
  icon: ReactNode
}

const primaryNavigation: NavigationItem[] = [
  { to: '/dashboard', label: 'Dashboard', icon: <HomeRegular /> },
  { to: '/gateways', label: 'Gateways', icon: <PlugConnectedRegular /> },
  { to: '/models', label: 'Model Foundry', icon: <CloudDatabaseRegular /> },
  { to: '/identity', label: 'Identity', icon: <PersonAccountsRegular /> },
  { to: '/entitlements', label: 'Entitlements', icon: <ShieldKeyholeRegular /> },
  { to: '/policies', label: 'Policies', icon: <CodeTextEditRegular /> },
  { to: '/analytics', label: 'Analytics', icon: <ChartMultipleRegular /> },
]

const utilityNavigation: NavigationItem[] = [
  { to: '/settings', label: 'Settings', icon: <SettingsRegular /> },
  { to: '/support', label: 'Support', icon: <QuestionCircleRegular /> },
]

function NavigationLinks({
  items,
  onNavigate,
}: {
  items: NavigationItem[]
  onNavigate: () => void
}) {
  return items.map((item) => (
    <NavLink
      key={item.to}
      to={item.to}
      className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
      onClick={onNavigate}
    >
      {item.icon}
      <span>{item.label}</span>
    </NavLink>
  ))
}

function AdminShell() {
  const { instance, accounts } = useMsal()
  const navigate = useNavigate()
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchMessage, setSearchMessage] = useState('')
  const accountName = accounts[0]?.name ?? 'MOSAIC administrator'
  const initials = useMemo(
    () =>
      accountName
        .split(/\s+/)
        .map((part) => part[0])
        .join('')
        .slice(0, 2)
        .toUpperCase(),
    [accountName],
  )

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    const query = searchQuery.trim().toLowerCase()
    if (!query) {
      return
    }
    const match = [...primaryNavigation, ...utilityNavigation].find((item) =>
      item.label.toLowerCase().includes(query),
    )
    if (match) {
      navigate(match.to)
      setSearchQuery('')
      setSearchMessage(`Opened ${match.label}`)
    } else {
      setSearchMessage(`No MOSAIC page matched "${searchQuery.trim()}"`)
    }
  }

  const closeNavigation = () => setMobileNavigationOpen(false)

  return (
    <div className="app-shell">
      <aside className={mobileNavigationOpen ? 'sidebar open' : 'sidebar'}>
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true">
            M
          </div>
          <div>
            <Title3 as="span">MOSAIC</Title3>
            <Text size={200}>AI gateway control plane</Text>
          </div>
          <Button
            className="sidebar-close"
            appearance="subtle"
            icon={<DismissRegular />}
            aria-label="Close navigation"
            onClick={closeNavigation}
          />
        </div>
        <nav className="primary-navigation" aria-label="Primary navigation">
          <NavigationLinks items={primaryNavigation} onNavigate={closeNavigation} />
        </nav>
        <nav className="utility-navigation" aria-label="Utility navigation">
          <NavigationLinks items={utilityNavigation} onNavigate={closeNavigation} />
          <button className="profile-quick-link" onClick={() => navigate('/profile')}>
            <span className="avatar">{initials}</span>
            <span>
              <strong>{accountName}</strong>
              <small>{runtimeConfig.authMode === 'entra' ? 'Global Admin' : 'Local mode'}</small>
            </span>
          </button>
        </nav>
      </aside>
      {mobileNavigationOpen && (
        <button
          className="navigation-scrim"
          aria-label="Close navigation"
          onClick={closeNavigation}
        />
      )}
      <header className="topbar">
        <Button
          className="mobile-menu"
          appearance="subtle"
          icon={<NavigationRegular />}
          aria-label="Open navigation"
          onClick={() => setMobileNavigationOpen(true)}
        />
        <form className="global-search" role="search" onSubmit={submitSearch}>
          <Input
            value={searchQuery}
            contentBefore={<SearchRegular />}
            placeholder="Search MOSAIC pages..."
            aria-label="Search MOSAIC pages"
            onChange={(_, data) => setSearchQuery(data.value)}
          />
        </form>
        <span className="sr-only" role="status">
          {searchMessage}
        </span>
        <div className="topbar-actions">
          <Button
            className="deploy-button"
            appearance="primary"
            icon={<AddRegular />}
            onClick={() => navigate('/models?deploy=1')}
          >
            Deploy Model
          </Button>
          <Menu>
            <MenuTrigger disableButtonEnhancement>
              <Tooltip content="Notifications" relationship="label">
                <Button appearance="subtle" icon={<AlertRegular />} aria-label="Notifications" />
              </Tooltip>
            </MenuTrigger>
            <MenuPopover>
              <MenuList>
                <MenuItem>No new notifications</MenuItem>
              </MenuList>
            </MenuPopover>
          </Menu>
          <Menu>
            <MenuTrigger disableButtonEnhancement>
              <Tooltip content="Recent activity" relationship="label">
                <Button appearance="subtle" icon={<HistoryRegular />} aria-label="Recent activity" />
              </Tooltip>
            </MenuTrigger>
            <MenuPopover>
              <MenuList>
                <MenuItem>Signed in to MOSAIC</MenuItem>
                <MenuItem onClick={() => navigate('/profile')}>View account activity</MenuItem>
              </MenuList>
            </MenuPopover>
          </Menu>
          <Tooltip content="Support" relationship="label">
            <Button
              appearance="subtle"
              icon={<QuestionCircleRegular />}
              aria-label="Support"
              onClick={() => navigate('/support')}
            />
          </Tooltip>
          <Menu>
            <MenuTrigger disableButtonEnhancement>
              <Button className="account-button" appearance="subtle" aria-label={`Account menu for ${accountName}`}>
                <span className="topbar-avatar">{initials}</span>
              </Button>
            </MenuTrigger>
            <MenuPopover>
              <MenuList>
                <MenuItem icon={<PersonCircleRegular />} onClick={() => navigate('/profile')}>
                  Admin Profile
                </MenuItem>
                {runtimeConfig.authMode === 'entra' && (
                  <MenuItem
                    icon={<SignOutRegular />}
                    onClick={() => void instance.logoutRedirect()}
                  >
                    Sign out
                  </MenuItem>
                )}
              </MenuList>
            </MenuPopover>
          </Menu>
        </div>
      </header>
      <main className="content">
        <div className="content-inner">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <MosaicThemeProvider>
      <AuthGate>
        <Routes>
          <Route element={<AdminShell />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/gateways" element={<GatewaysPage />} />
            <Route path="/gateways/:gatewayId" element={<GatewayDetailPage />} />
            <Route path="/models" element={<ModelFoundryPage />} />
            <Route path="/identity" element={<IdentityPage />} />
            <Route path="/principals" element={<Navigate to="/identity?tab=users" replace />} />
            <Route path="/groups" element={<Navigate to="/identity?tab=groups" replace />} />
            <Route path="/entitlements" element={<EntitlementsPage />} />
            <Route path="/policies" element={<PoliciesPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/usage" element={<Navigate to="/analytics" replace />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/support" element={<SupportPage />} />
            <Route path="/profile" element={<AdminProfilePage />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </MosaicThemeProvider>
  )
}
