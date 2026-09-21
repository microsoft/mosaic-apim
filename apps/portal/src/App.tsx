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
  DismissRegular,
  HomeRegular,
  NavigationRegular,
  PersonCircleRegular,
  SignOutRegular,
} from '@fluentui/react-icons'
import { useMsal } from '@azure/msal-react'
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useMemo, useState } from 'react'
import { NavLink, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { usePortalApi } from './api'
import { AuthGate } from './auth'
import { ErrorState, Loading } from './components/AsyncState'
import { CatalogPage } from './pages/CatalogPage'
import { MyAccessPage } from './pages/MyAccessPage'
import { MyRequestsPage } from './pages/MyRequestsPage'
import { runtimeConfig } from './runtime-config'
import { MosaicThemeProvider } from './theme'
import './index.css'

interface NavigationItem {
  to: string
  label: string
  icon: ReactNode
}

const navigation: NavigationItem[] = [
  { to: '/access', label: 'My access', icon: <HomeRegular /> },
  { to: '/catalog', label: 'Catalog', icon: <BookInformationRegular /> },
  { to: '/requests', label: 'My requests', icon: <PersonCircleRegular /> },
]

function NavigationLinks({ onNavigate }: { onNavigate: () => void }) {
  return navigation.map((item) => (
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

function initialsFor(label: string) {
  return label
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

function PortalShell() {
  const { instance, accounts } = useMsal()
  const api = usePortalApi()
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false)
  const profile = useQuery({ queryKey: ['portal', 'profile'], queryFn: api.getProfile })
  const accountName = profile.data?.displayLabel ?? accounts[0]?.name ?? 'MOSAIC user'
  const initials = useMemo(() => initialsFor(accountName), [accountName])
  const closeNavigation = () => setMobileNavigationOpen(false)
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
    <div className="app-shell">
      <aside className={mobileNavigationOpen ? 'sidebar open' : 'sidebar'}>
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true">M</div>
          <div>
            <Title3 as="span">MOSAIC</Title3>
            <Text size={200}>End-user portal</Text>
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
          <NavigationLinks onNavigate={closeNavigation} />
        </nav>
      </aside>
      {mobileNavigationOpen && (
        <button className="navigation-scrim" aria-label="Close navigation" onClick={closeNavigation} />
      )}
      <header className="topbar">
        <Button
          className="mobile-menu"
          appearance="subtle"
          icon={<NavigationRegular />}
          aria-label="Open navigation"
          onClick={() => setMobileNavigationOpen(true)}
        />
        <div className="signed-in-summary">
          <span className="topbar-avatar">{initials}</span>
          <span>
            <strong>{accountName}</strong>
            <small>
              {profile.isLoading ? 'Checking access' : `${profile.data?.entitlementCount ?? 0} entitlements · ${profile.data?.pendingRequestCount ?? 0} pending requests`}
            </small>
          </span>
          {profile.data?.isAdmin && <Badge appearance="tint">Admin allowed</Badge>}
        </div>
        <div className="topbar-actions">
          {profile.isLoading && <Loading label="Loading profile" />}
          <Menu>
            <MenuTrigger disableButtonEnhancement>
              <Button className="account-button" appearance="subtle" aria-label={`Account menu for ${accountName}`}>
                <PersonCircleRegular />
              </Button>
            </MenuTrigger>
            <MenuPopover>
              <MenuList>
                <MenuItem>{accountName}</MenuItem>
                {runtimeConfig.authMode === 'entra' && (
                  <MenuItem icon={<SignOutRegular />} onClick={signOut}>
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
          <Route element={<PortalShell />}>
            <Route index element={<Navigate to="/access" replace />} />
            <Route path="/access" element={<MyAccessPage />} />
            <Route path="/catalog" element={<CatalogPage />} />
            <Route path="/requests" element={<MyRequestsPage />} />
            <Route path="*" element={<Navigate to="/access" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </MosaicThemeProvider>
  )
}
