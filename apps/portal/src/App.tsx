import {
  Badge,
  Button,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Text,
  Title3,
} from '@fluentui/react-components'
import {
  BookInformationRegular,
  DataUsageRegular,
  DismissRegular,
  HomeRegular,
  NavigationRegular,
  PersonCircleRegular,
  SignOutRegular,
  ChevronLeftRegular,
  ChevronRightRegular,
} from '@fluentui/react-icons'
import { useMsal } from '@azure/msal-react'
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useCallback, useMemo, useState } from 'react'
import { NavLink, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { usePortalApi } from './api'
import { AuthGate } from './auth'
import { ErrorState, Loading } from './components/AsyncState'
import { initialsFor } from './initials'
import { CatalogPage } from './pages/CatalogPage'
import { MyAccessPage } from './pages/MyAccessPage'
import { MyRequestsPage } from './pages/MyRequestsPage'
import { UsagePage } from './pages/UsagePage'
import { runtimeConfig } from './runtime-config'
import { MosaicThemeProvider } from './theme'
import { formatCount } from './usage-format'
import { MosaicMark } from './components/MosaicMark'
import { useNavigationDrawer } from './components/useNavigationDrawer'
import './index.css'

interface NavigationItem {
  to: string
  label: string
  icon: ReactNode
}

const navigation: NavigationItem[] = [
  { to: '/usage', label: 'Usage & cost', icon: <DataUsageRegular /> },
  { to: '/catalog', label: 'Catalog', icon: <BookInformationRegular /> },
  { to: '/access', label: 'My access', icon: <HomeRegular /> },
  { to: '/requests', label: 'My requests', icon: <PersonCircleRegular /> },
]

function NavigationLinks({ onNavigate }: { onNavigate: () => void }) {
  return navigation.map((item) => (
    <NavLink
      key={item.to}
      to={item.to}
      className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
      onClick={onNavigate}
      title={item.label}
      aria-label={item.label}
    >
      {item.icon}
      <span>{item.label}</span>
    </NavLink>
  ))
}

function PortalShell() {
  const { instance, accounts } = useMsal()
  const api = usePortalApi()
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('mosaic.portal.primary.collapsed') === 'true')
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const accountName = profile.data?.displayLabel ?? accounts[0]?.name ?? 'MOSAIC user'
  const initials = useMemo(() => initialsFor(accountName), [accountName])
  const grants = formatCount(profile.data?.entitlementCount ?? 0, 'grant')
  const pendingRequests = formatCount(profile.data?.pendingRequestCount ?? 0, 'pending request')
  const closeNavigation = useCallback(() => setMobileNavigationOpen(false), [])
  const navigationRef = useNavigationDrawer(mobileNavigationOpen, closeNavigation)
  const signOut = () => void instance.logoutRedirect()

  if (profile.isError) {
    return (
      <div className="centered-page">
        <div className="access-denied-card">
          <ErrorState error={profile.error} />
          {runtimeConfig.authMode === 'entra' && (
            <Button className="standalone-signout" onClick={signOut}>
              Sign out
            </Button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className={`app-shell ${collapsed ? 'rail-collapsed' : ''}`}>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside ref={navigationRef} className={mobileNavigationOpen ? 'sidebar open' : 'sidebar'}
        role={mobileNavigationOpen ? 'dialog' : undefined} aria-modal={mobileNavigationOpen || undefined}
        aria-label={mobileNavigationOpen ? 'Navigation' : undefined}>
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true"><MosaicMark /></div>
          <div>
            <Title3 as="span">MOSAIC</Title3>
            <Text size={200}>End-user portal</Text>
          </div>
          <Button className="rail-toggle" appearance="subtle" icon={collapsed ? <ChevronRightRegular /> : <ChevronLeftRegular />}
            aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'} aria-expanded={!collapsed}
            onClick={() => {
              const next = !collapsed
              localStorage.setItem('mosaic.portal.primary.collapsed', String(next))
              setCollapsed(next)
            }} />
          <Button
            className="sidebar-close"
            appearance="subtle"
            icon={<DismissRegular />}
            aria-label="Close navigation"
            onClick={closeNavigation}
          />
        </div>
        <nav className="primary-navigation" aria-label="Primary navigation">
          <NavigationLinks onNavigate={closeNavigation} />
        </nav>
        <div className="utility-navigation">
          <Menu>
            <MenuTrigger disableButtonEnhancement>
              <Button className="profile-quick-link" appearance="subtle" aria-label={`Account menu for ${accountName}`} title={accountName}>
                <span className="topbar-avatar">{initials}</span>
                <span>{accountName}</span>
              </Button>
            </MenuTrigger>
            <MenuPopover><MenuList>
              <MenuItem>{accountName}</MenuItem>
              {runtimeConfig.authMode === 'entra' && <MenuItem icon={<SignOutRegular />} onClick={signOut}>Sign out</MenuItem>}
            </MenuList></MenuPopover>
          </Menu>
        </div>
      </aside>
      {mobileNavigationOpen && (
        <button className="navigation-scrim" aria-label="Close navigation" onClick={closeNavigation} />
      )}
      <header className="topbar" inert={mobileNavigationOpen}>
        <Button
          className="mobile-menu"
          appearance="subtle"
          icon={<NavigationRegular />}
          aria-label="Open navigation"
          onClick={() => setMobileNavigationOpen(true)}
        />
        <div className="signed-in-summary">
          <span>
            <strong>Your workspace</strong>
            <small>{profile.isLoading ? 'Checking access' : `${grants} · ${pendingRequests}`}</small>
          </span>
        </div>
        <div className="topbar-actions">
          {profile.isLoading && <Loading label="Loading profile" />}
          {profile.isSuccess && profile.data.pendingRequestCount > 0 &&
            <NavLink className="pending-link" to="/requests"><Badge appearance="tint">{pendingRequests}</Badge></NavLink>}
        </div>
      </header>
      <main id="main-content" className="content" inert={mobileNavigationOpen}>
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
          <Route element={<PortalShell />}>
            <Route index element={<Navigate to="/usage" replace />} />
            <Route path="/access" element={<MyAccessPage />} />
            <Route path="/usage" element={<UsagePage />} />
            <Route path="/catalog" element={<CatalogPage />} />
            <Route path="/requests" element={<MyRequestsPage />} />
            <Route path="*" element={<Navigate to="/usage" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </MosaicThemeProvider>
  )
}
