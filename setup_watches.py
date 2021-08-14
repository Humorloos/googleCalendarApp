"""
Script for setting up google calendar watches for specified time and save map from uuids to calendar ids
"""
import datetime as dt
import uuid

import pandas as pd

import googleApiScopes.calendar
from googleApiClientProvider import GoogleApiClientProvider
from utils import get_calendar_ids, CALENDAR_LOOKUP_PATH, get_calendar_lookup

SCOPES = [googleApiScopes.calendar.EVENTS, googleApiScopes.calendar.CALENDAR_READ_ONLY]
WATCH_DURATION = str(int(dt.timedelta(days=1, hours=6).total_seconds()))

client_provider = GoogleApiClientProvider(SCOPES)
calendar_service = client_provider.get_service(name="calendar", version='v3')

# Close old channels
calendar_lookup = get_calendar_lookup()

for channel_id, row in calendar_lookup.iterrows():
    calendar_service.channels().stop(body={'id': channel_id, 'resourceId': row['resource_id']})

# Open new channels
calendar_ids = get_calendar_ids(calendar_service)
calendar_lookup = calendar_lookup[calendar_lookup['calendar_id'].isin(calendar_ids)]
if len(calendar_ids) > len(calendar_lookup):
    calendar_lookup = calendar_lookup.append(
        {'calendar_id': calendar_id for calendar_id in calendar_ids if
         calendar_id not in calendar_lookup['calendar_id'].values}, ignore_index=True
    )
calendar_lookup.index = pd.Series([str(uuid.uuid1()) for _ in calendar_lookup.index], name='channel_id')

responses = [calendar_service.events().watch(
    calendarId=row['calendar_id'],
    body={
        "id": channel_id,
        "token": "my token",
        "type": "web_hook",
        "address": "https://humorloos.pythonanywhere.com/",
        "params": {
            "ttl": WATCH_DURATION
        }
    }
).execute() for channel_id, row in calendar_lookup.iterrows()]

calendar_lookup['resource_id'] = [response['resourceId'] for response in responses]
calendar_lookup.to_csv(CALENDAR_LOOKUP_PATH)