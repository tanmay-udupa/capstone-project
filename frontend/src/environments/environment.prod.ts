export const environment = {
  production: true,
  apiBaseUrl: 'https://capstone-backend-api.azurewebsites.net',
  msalConfig: {
    auth: {
      clientId: 'a3e163cd-830c-4b9c-be5e-446930fcfa6f',
      authority: 'https://login.microsoftonline.com/425a5546-5a6e-4f1b-ab62-23d91d07d893',
      redirectUri: 'https://white-sky-0d2e9850f.7.azurestaticapps.net',
    },
  },
  apiScopes: ['api://a3e163cd-830c-4b9c-be5e-446930fcfa6f/access_as_user'],
};
