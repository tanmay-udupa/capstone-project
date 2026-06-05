export const environment = {
  production: false,
  apiBaseUrl: 'http://localhost:8000',
  msalConfig: {
    auth: {
      clientId: 'a3e163cd-830c-4b9c-be5e-446930fcfa6f',
      authority: 'https://login.microsoftonline.com/425a5546-5a6e-4f1b-ab62-23d91d07d893',
      redirectUri: 'http://localhost:4200',
    },
  },
  apiScopes: ['api://a3e163cd-830c-4b9c-be5e-446930fcfa6f/access_as_user'],
};
