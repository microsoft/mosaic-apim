import {
  Button,
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
  ChartMultipleRegular,
  CloudDatabaseRegular,
  CodeTextEditRegular,
  BuildingRegular,
  MoneyRegular,
  DismissRegular,
  HomeRegular,
  NavigationRegular,
  PersonAccountsRegular,
  PersonCircleRegular,
  PlugConnectedRegular,
  PuzzlePieceRegular,
  QuestionCircleRegular,
  ChevronLeftRegular,
  ChevronRightRegular,
  SettingsRegular,
  ShieldKeyholeRegular,
  SignOutRegular,
  StackRegular,
} from '@fluentui/react-icons'
import { useMsal } from '@azure/msal-react'
import { type ReactNode, useCallback, useState } from 'react'
import {
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
import { AuthGate } from './auth'
import { ConsoleAccessGate } from './console-access'
import { AdminProfilePage } from './pages/AdminProfilePage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { CostCenterDetailPage, CostCentersPage } from './pages/CostCentersPage'
import { DashboardPage } from './pages/DashboardPage'
import { EntitlementsPage } from './pages/EntitlementsPage'
import { GatewayDetailPage } from './pages/GatewayDetailPage'
import { GatewaysPage } from './pages/GatewaysPage'
import { IdentityPage } from './pages/IdentityPage'
import { McpsPage } from './pages/McpsPage'
import { ModelsPage } from './pages/ModelsPage'
import { PoliciesPage } from './pages/PoliciesPage'
import { PoolDetailPage } from './pages/PoolDetailPage'
import { PoolsPage } from './pages/PoolsPage'
import { PricingPage } from './pages/PricingPage'
import { SettingsPage } from './pages/SettingsPage'
import { SupportPage } from './pages/SupportPage'
import { runtimeConfig } from './runtime-config'
import { MosaicThemeProvider } from './theme'
import { MosaicMark } from './components/MosaicMark'
import { PageSearch } from './components/PageSearch'
import { RequestNotifications } from './components/RequestNotifications'
import { NotificationBell, NotificationInboxPage } from './components/NotificationInbox'
import { useNavigationDrawer } from './components/useNavigationDrawer'
import { AppearanceControl } from './components/AppearanceControl'
import { IdentityAvatar } from './components/IdentityAvatar'
import './index.css'

interface NavigationItem {
  to: string
  label: string
  icon: ReactNode
}

const primaryNavigation: NavigationItem[] = [
  { to: '/dashboard', label: 'Dashboard', icon: <HomeRegular /> },
  { to: '/gateways', label: 'Gateways', icon: <PlugConnectedRegular /> },
  { to: '/models', label: 'Models', icon: <CloudDatabaseRegular /> },
  { to: '/pools', label: 'Pools', icon: <StackRegular /> },
  { to: '/mcps', label: 'MCPs', icon: <PuzzlePieceRegular /> },
  { to: '/identity', label: 'Identity', icon: <PersonAccountsRegular /> },
  { to: '/entitlements', label: 'Entitlements', icon: <ShieldKeyholeRegular /> },
  { to: '/cost-centers', label: 'Cost centers', icon: <BuildingRegular /> },
  { to: '/policies', label: 'Policies', icon: <CodeTextEditRegular /> },
  { to: '/analytics', label: 'Analytics', icon: <ChartMultipleRegular /> },
  { to: '/pricing', label: 'Pricing', icon: <MoneyRegular /> },
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
    <Tooltip key={item.to} content={item.label} relationship="label">
    <NavLink
      to={item.to}
      className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
      onClick={onNavigate}
      title={item.label}
      aria-label={item.label}
    >
      {item.icon}
      <span>{item.label}</span>
    </NavLink>
    </Tooltip>
  ))
}

function AdminShell() {
  const { instance, accounts } = useMsal()
  const navigate = useNavigate()
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('mosaic.primary.collapsed') === 'true')
  const accountName = accounts[0]?.name ?? 'MOSAIC administrator'
  const accountObjectId = accounts[0]?.localAccountId

  const closeNavigation = useCallback(() => setMobileNavigationOpen(false), [])
  const navigationRef = useNavigationDrawer(mobileNavigationOpen, closeNavigation)
  function toggleCollapse() {
    const next = !collapsed
    localStorage.setItem('mosaic.primary.collapsed', String(next))
    setCollapsed(next)
  }
  const searchDestinations = [
    ...primaryNavigation, ...utilityNavigation,
    { label: 'Models · Published models', to: '/models?tab=published' },
    { label: 'Models · Imported model APIs', to: '/models?tab=imported' },
    { label: 'Models · Model endpoints', to: '/models?tab=endpoints' },
    { label: 'Entitlements · Access requests', to: '/entitlements?tab=requests' },
    { label: 'Entitlements · Model access settings', to: '/entitlements?tab=settings' },
    { label: 'Entitlements · Approval policies', to: '/entitlements?tab=approvalPolicies' },
    { label: 'Settings · AI approval assistance', to: '/settings?tab=approvalAi' },
    { label: 'Settings · Notifications', to: '/settings?tab=notifications' },
    { label: 'Notification inbox', to: '/notifications' },
    { label: 'Pricing · Unpriced deployments', to: '/pricing?tab=unpriced' },
    { label: 'Settings · Appearance', to: '/settings?tab=appearance' },
  ]

  return (
    <div className={`app-shell ${collapsed ? 'rail-collapsed' : ''}`}>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside ref={navigationRef} className={mobileNavigationOpen ? 'sidebar open' : 'sidebar'}
        role={mobileNavigationOpen ? 'dialog' : undefined} aria-modal={mobileNavigationOpen || undefined}
        aria-label={mobileNavigationOpen ? 'Navigation' : undefined}>
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true">
            <MosaicMark />
          </div>
          <div>
            <Title3 as="span">MOSAIC</Title3>
            <Text size={200}>AI gateway control plane</Text>
          </div>
          <Button className="rail-toggle" appearance="subtle" icon={collapsed ? <ChevronRightRegular /> : <ChevronLeftRegular />}
            aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'} aria-expanded={!collapsed} onClick={toggleCollapse} />
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
          <AppearanceControl />
          <Menu>
            <MenuTrigger disableButtonEnhancement>
          <button className="profile-quick-link" aria-label={`Account menu for ${accountName}`} title={accountName}>
            <IdentityAvatar name={accountName} objectId={accountObjectId} size={32} />
            <span>
              <strong>{accountName}</strong>
              {/* In Entra mode ConsoleAccessGate renders the shell only once the API confirms the Admin role. */}
              <small>{runtimeConfig.authMode === 'entra' ? 'MOSAIC Admin' : 'Local mode'}</small>
            </span>
          </button>
            </MenuTrigger>
            <MenuPopover><MenuList>
              <MenuItem icon={<PersonCircleRegular />} onClick={() => { closeNavigation(); navigate('/profile') }}>Admin Profile</MenuItem>
              {runtimeConfig.authMode === 'entra' && <MenuItem icon={<SignOutRegular />} onClick={() => void instance.logoutRedirect()}>Sign out</MenuItem>}
            </MenuList></MenuPopover>
          </Menu>
        </nav>
      </aside>
      {mobileNavigationOpen && (
        <button
          className="navigation-scrim"
          aria-label="Close navigation"
          onClick={closeNavigation}
        />
      )}
      <header className="topbar" inert={mobileNavigationOpen}>
        <Button
          className="mobile-menu"
          appearance="subtle"
          icon={<NavigationRegular />}
          aria-label="Open navigation"
          onClick={() => setMobileNavigationOpen(true)}
        />
        <PageSearch destinations={searchDestinations} />
        <div className="topbar-actions">
          <RequestNotifications />
          <NotificationBell />
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
        <ConsoleAccessGate>
          <Routes>
            <Route element={<AdminShell />}>
              <Route index element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/gateways" element={<GatewaysPage />} />
              <Route path="/gateways/:gatewayId" element={<GatewayDetailPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/pools" element={<PoolsPage />} />
              <Route path="/pools/:poolId" element={<PoolDetailPage />} />
              <Route path="/mcps" element={<McpsPage />} />
              <Route path="/identity" element={<IdentityPage />} />
              <Route path="/principals" element={<Navigate to="/identity?tab=users" replace />} />
              <Route path="/groups" element={<Navigate to="/identity?tab=groups" replace />} />
              <Route path="/entitlements" element={<EntitlementsPage />} />
              <Route path="/cost-centers" element={<CostCentersPage />} />
              <Route path="/cost-centers/:costCenterId" element={<CostCenterDetailPage />} />
              <Route path="/policies" element={<PoliciesPage />} />
              <Route path="/analytics" element={<AnalyticsPage />} />
              <Route path="/usage" element={<Navigate to="/analytics" replace />} />
              <Route path="/pricing" element={<PricingPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/support" element={<SupportPage />} />
              <Route path="/profile" element={<AdminProfilePage />} />
              <Route path="/notifications" element={<NotificationInboxPage />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Route>
          </Routes>
        </ConsoleAccessGate>
      </AuthGate>
    </MosaicThemeProvider>
  )
}
