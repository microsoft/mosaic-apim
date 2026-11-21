import { Button, MessageBar, MessageBarBody, Spinner, Switch, Text } from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useMosaicApi } from '../../api'
import { DEFAULT_PALETTES, normalizeScheme, type OrganizationScheme, paletteIsReadable, type ThemeBase } from '../../appearance'
import { useMosaicTheme } from '../../theme-context'
import { ErrorState } from '../AsyncState'
import { appearanceQueryKey } from './OrganizationAppearanceLoader'
import { PaletteEditor } from './PaletteEditor'
import styles from './appearance.module.css'

const MODES: Array<{ value: ThemeBase; label: string }> = [
  { value: 'light', label: 'Light mode colours' },
  { value: 'dark', label: 'Dark mode colours' },
]

/** The colours everyone in the organization sees in Light and Dark, in the console and the portal. */
export function OrganizationAppearanceEditor() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const { setOrganization } = useMosaicTheme()
  const saved = useQuery({ queryKey: ['appearance', 'admin'], queryFn: api.getAppearance })
  const [custom, setCustom] = useState(false)
  const [scheme, setScheme] = useState<OrganizationScheme>(DEFAULT_PALETTES)
  const [allowCustomThemes, setAllowCustomThemes] = useState(true)
  const [mode, setMode] = useState<ThemeBase>('light')

  useEffect(() => {
    if (!saved.data) return
    const stored = normalizeScheme(saved.data.scheme)
    setCustom(stored !== null)
    setScheme(stored ?? DEFAULT_PALETTES)
    setAllowCustomThemes(saved.data.allowCustomThemes)
  }, [saved.data])

  const save = useMutation({
    mutationFn: () => api.updateAppearance({ scheme: custom ? scheme : null, allowCustomThemes }),
    onSuccess: (result) => {
      queryClient.setQueryData(['appearance', 'admin'], result)
      void queryClient.invalidateQueries({ queryKey: appearanceQueryKey, exact: true })
      // Show the administrator the result at once; everyone else gets it the next time their app loads.
      setOrganization({ scheme: normalizeScheme(result.scheme), allowCustomThemes: result.allowCustomThemes })
    },
  })

  if (saved.isLoading) return <Spinner size="tiny" label="Loading organization colours" />
  if (saved.isError) return <ErrorState title="Organization colours unavailable" error={saved.error} />

  const readable = !custom || (paletteIsReadable(scheme.light) && paletteIsReadable(scheme.dark))
  const unreadable = custom ? MODES.filter((item) => !paletteIsReadable(scheme[item.value])).map((item) => item.label.toLowerCase()) : []

  return (
    <div className={styles.stack}>
      <Switch checked={custom} onChange={(_, data) => setCustom(data.checked)} label="Use our own colours instead of MOSAIC's" />
      {custom && (
        <>
          <div className={styles.modeTabs} role="group" aria-label="Palette to edit">
            {MODES.map((item) => (
              <Button key={item.value} size="small" appearance={mode === item.value ? 'primary' : 'subtle'} aria-pressed={mode === item.value}
                onClick={() => setMode(item.value)}>{item.label}</Button>
            ))}
          </div>
          <PaletteEditor key={mode} palette={scheme[mode]} base={mode} onChange={(palette) => setScheme({ ...scheme, [mode]: palette })} />
          {unreadable.length > 0 && (
            <MessageBar intent="warning">
              <MessageBarBody>Some text would be hard to read in the {unreadable.join(' and the ')}. Adjust the colours marked there before saving.</MessageBarBody>
            </MessageBar>
          )}
        </>
      )}
      <Switch checked={allowCustomThemes} onChange={(_, data) => setAllowCustomThemes(data.checked)} label="Let people choose their own custom colours" />
      <Text className={styles.note}>
        Saving changes Light and Dark for everyone in the console and the portal the next time their app loads.
        A person&apos;s own custom colours stay in their browser and are not affected.
      </Text>
      {save.isError && <ErrorState context="action" title="Unable to save organization colours" error={save.error} />}
      {save.isSuccess && !save.isPending && (
        <MessageBar intent="success"><MessageBarBody>Organization colours saved.</MessageBarBody></MessageBar>
      )}
      <div className={styles.row}>
        <Button appearance="primary" disabled={!readable || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : 'Save organization colours'}
        </Button>
      </div>
    </div>
  )
}
