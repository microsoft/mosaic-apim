import { Field, Input, Select, Text } from '@fluentui/react-components'
import type { VertexConfiguration } from '../types'
import styles from '../pages/ModelsPage.module.css'

export function VertexEndpointFields({ value, onChange }: {
  value: VertexConfiguration
  onChange: (value: VertexConfiguration) => void
}) {
  return <>
    <Field label="Google Cloud project ID" required>
      <Input required value={value.projectId} spellCheck={false}
        onChange={(_, data) => onChange({ ...value, projectId: data.value })} />
    </Field>
    <Field label="Location and API">
      <Text>U.S. multi-region (us), native v1. The host is fixed to aiplatform.us.rep.googleapis.com.
        No global or single-region fallback.</Text>
    </Field>
    <Field label="Google authentication">
      <Select className={styles.vertexAuthSelect} value={value.authMode} onChange={(event) => {
        const authMode = event.target.value as VertexConfiguration['authMode']
        onChange({ ...value, authMode, serviceAccount: undefined, workloadIdentityAudience: undefined })
      }}>
        <option value="workloadIdentityFederation">Keyless workload identity federation</option>
        <option value="serviceAccountKey">Key Vault-held service-account signing credential</option>
        <option value="apiKey">Google authorization key (conditional support)</option>
      </Select>
    </Field>
    {value.authMode !== 'apiKey' && <Field label="Google service account" required>
      <Input required value={value.serviceAccount ?? ''} spellCheck={false}
        onChange={(_, data) => onChange({ ...value, serviceAccount: data.value })} />
    </Field>}
    {value.authMode === 'workloadIdentityFederation' && <Field label="Google federation audience" required
      hint="The configured Google workload identity provider audience. This is not a token or a credential-source URL.">
      <Input required value={value.workloadIdentityAudience ?? ''} spellCheck={false}
        onChange={(_, data) => onChange({ ...value, workloadIdentityAudience: data.value })} />
    </Field>}
    <Field label="Declared native model">
      <Text>gemini-3.8-flash — generateContent and streamGenerateContent.
        Declaring this model does not prove availability or permission.</Text>
    </Field>
  </>
}

export function ExternalEndpointGuidance({ vertex }: { vertex: boolean }) {
  return <>
    <h3>Save configuration, not access</h3>
    <Text block>Registration stores an endpoint, credential references and exact model declarations.
      It makes no provider calls, deploys nothing and grants nobody access.</Text>
    <h3>{vertex ? 'Google processes requests' : 'The approved backend processes requests'}</h3>
    <Text block>Prompts, inline images and application-executed function messages leave MOSAIC’s
      Azure boundary. Remote image references and provider-hosted tools are outside this release.</Text>
    <h3>Evidence before publication</h3>
    <Text block>{vertex
      ? 'Federation, signing credentials and Google authorization keys are separate contracts. Gateway authentication, native streaming and accounting remain unverified; unsupported execution stays blocked.'
      : 'Only explicitly admitted public HTTPS destinations are eligible. Host admission is not proof of gateway egress or DNS-rebinding protection; unverified execution stays blocked.'}</Text>
    <h3>Grant and connect separately</h3>
    <Text block>Review and Apply publication are explicit later steps. An approved grant also needs
      reviewed and applied enforcement before its caller connection can be ready.
      Caller subscription keys never authenticate to Google or the custom backend.</Text>
  </>
}
