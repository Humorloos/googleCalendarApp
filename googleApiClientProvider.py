import os.path
from functools import cached_property

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from calendar_service import CalendarService
from utils import TOKEN_PATH, CREDENTIALS_PATH


class GoogleApiClientProvider:

    def __init__(self, scopes):
        self.scopes = scopes

    @cached_property
    def credentials(self):
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        print(f'Trying to fetch token from {TOKEN_PATH.absolute()}')
        if os.path.exists(TOKEN_PATH):
            print('Token found, using token for authorization')
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, self.scopes)
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print('Token is expired, refreshing token')
                creds.refresh(Request())
            else:
                print(f'Token not found, creating new token from credentials at {CREDENTIALS_PATH.absolute()}')
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, self.scopes)
                creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open(TOKEN_PATH, 'w') as token:
                print(f'Saving new token to {CREDENTIALS_PATH.absolute()}')
                token.write(creds.to_json())
        return creds

    def get_service(self, name: str, version: str):
        return build(name, version, credentials=self.credentials)

    def get_calendar_service(self):
        return CalendarService(self.get_service('calendar', 'v3'))
