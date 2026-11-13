// This module is aliased only by the isolated screenshot fixture, never by either product build.
export function useMsal() {
  return {
    accounts: [{
      homeAccountId: 'fictional-screenshot-user',
      name: 'Megan Bowen',
      username: 'megan.bowen@contoso.example.test',
    }],
    instance: {
      acquireTokenSilent: async () => ({ accessToken: 'fictional-screenshot-token' }),
      logoutRedirect: async () => {
        document.body.dataset.screenshotSignedOut = 'true'
      },
    },
  }
}
