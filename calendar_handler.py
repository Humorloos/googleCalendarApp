import datetime as dt
import logging
from functools import cached_property
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import pandas as pd
from flask import Flask, request
from flask_restful import Resource, Api

import googleApiScopes.calendar
from googleApiClientProvider import GoogleApiClientProvider
from utils import get_calendar_lookup, CALENDAR_LOOKUP_PATH, PROJECT_SUFFIX, event_row_to_body

FEIERABEND = dt.time(20)

CHANNEL_ID_KEY = 'X-Goog-Channel-Id'
SCOPES = [googleApiScopes.calendar.EVENTS, googleApiScopes.calendar.CALENDAR_READ_ONLY]
# Color ID that shall trigger splitting events (see https://lukeboyle.com/blog/posts/google-calendar-api-color-id)
SPLIT_COLOR_ID = '8'
OPTIONAL_EVENT_FIELDS = ('description', 'location')

app = Flask(__name__)
api = Api(app)

# setup logger
info_filehandler = RotatingFileHandler(Path('logs').joinpath('app.log'), maxBytes=10000000, backupCount=3)
info_filehandler.setLevel(logging.INFO)
logger = logging.getLogger()
logger.addHandler(info_filehandler)
logger.setLevel(logging.INFO)

# setup google calendar interface
api_provider = GoogleApiClientProvider(SCOPES)
calendar_service = api_provider.get_calendar_service()


# log all requests
@app.before_request
def log_request_info():
    logger.info(f'Headers: {request.headers}')
    logger.info(f'Body: {request.get_data()}')


class CalendarHandler(Resource):
    def __init__(self):
        self.next_sync_token = None

    @cached_property
    def calendar_lookup(self):
        return get_calendar_lookup()

    @staticmethod
    def get():
        return 'This page only exists to handle calendar updates'

    def post(self):
        # only react to calendar API channel posts
        if CHANNEL_ID_KEY not in request.headers.keys():
            return
        channel_id = request.headers.get(CHANNEL_ID_KEY)

        # only react to channels in lookup table
        if channel_id not in self.calendar_lookup.index:
            return

        # get updated events
        calendar_row = self.calendar_lookup.loc[channel_id]
        target_calendar_id = calendar_row['calendar_id']
        updated_events, self.next_sync_token = calendar_service.get_event_df_and_next_sync_token(
            sync_token=calendar_row['sync_token'],
            calendar_id=target_calendar_id,
        )

        if len(updated_events) > 0 and 'updated' in updated_events.columns:
            updated_events = updated_events[updated_events['status'] == 'confirmed'].sort_values(
                by='updated', ascending=False, ignore_index=True)
            # remember projects that were already updated to update each project only once
            updated_projects = set()
            for _, updated_event in updated_events.iterrows():
                # only trigger updates for events that belong to a project that hasn't been updated so far
                if updated_event['summary'].endswith(PROJECT_SUFFIX) and \
                        updated_event['summary'] not in updated_projects:

                    # get events that belong to this project
                    project_events, self.next_sync_token = calendar_service.get_event_df_and_next_sync_token(
                        calendar_id=target_calendar_id,
                        query=updated_event['summary'][:-3],
                    )

                    # update all events in this project except the event that triggered the update
                    new_description = updated_event['description']
                    for _, project_event in project_events.iterrows():
                        if project_event['id'] != updated_event['id'] and \
                                project_event['summary'] == updated_event['summary'] and \
                                project_event['description'] != new_description:
                            project_event['description'] = new_description
                            calendar_service.service.events().update(
                                calendarId=target_calendar_id,
                                eventId=project_event['id'],
                                body=event_row_to_body(project_event),
                            ).execute()

                    updated_projects.add(updated_event['summary'])

                # Check if event is supposed to be split (or moved)
                if 'colorId' in updated_event.index and updated_event['colorId'] == SPLIT_COLOR_ID:
                    self.split_or_move_event(target_calendar_id, updated_event)

        # remember the last time we retrieved events for next update
        self.calendar_lookup.loc[channel_id, 'sync_token'] = self.next_sync_token
        self.calendar_lookup.to_csv(CALENDAR_LOOKUP_PATH)
        # invalidate calendar lookup cache
        del self.__dict__['calendar_lookup']

    def split_or_move_event(self, target_calendar_id, updated_event):
        start_timestamp, end_timestamp = (
            calendar_service.local_datetime_from_string(updated_event[field]['dateTime'])
            for field in ('start', 'end')
        )
        feierabend_timestamp = calendar_service.get_local_datetime(end_timestamp.date(), FEIERABEND)
        split_timestamp: Optional[pd.Timestamp] = None
        # if event is longer than Feierabend, split at Feierabend
        if end_timestamp > feierabend_timestamp:
            split_timestamp = feierabend_timestamp
        # otherwise split at next interrupting event or move event (set split timestamp to start of updated
        # event)
        else:
            interrupting_events = pd.DataFrame()
            for calendar_id in self.calendar_lookup['calendar_id'].values:
                events_to_append, self.next_sync_token = calendar_service.get_event_df_and_next_sync_token(
                    calendar_id=calendar_id,
                    time_max=updated_event['end']['dateTime'],
                    time_min=updated_event['start']['dateTime'],
                )
                interrupting_events = interrupting_events.append(events_to_append)
            interrupting_events = interrupting_events[
                (interrupting_events['id'] != updated_event['id']) &
                (interrupting_events['start'].apply(lambda start_dict: 'dateTime' in start_dict.keys()))
                ]

            if len(interrupting_events) > 0:
                split_timestamp = max(
                    interrupting_events['start'].apply(
                        calendar_service.extract_local_datetime_or_nat).min(),
                    start_timestamp,
                )
        # perform splitting or moving
        if split_timestamp is not None:
            # if event shall be moved, remove event
            if split_timestamp == start_timestamp:
                calendar_service.service.events().delete(
                    calendarId=target_calendar_id,
                    eventId=updated_event['id'],
                ).execute()
            # otherwise update event to end at split timestamp
            else:
                updated_event = event_row_to_body(updated_event)
                del updated_event['colorId']
                updated_event['end']['dateTime'] = split_timestamp.isoformat()
                calendar_service.service.events().update(
                    calendarId=target_calendar_id,
                    eventId=updated_event['id'],
                    body=updated_event
                ).execute()

            # create new events starting at next free position and ending after time cut from original
            # event that is otherwise identical
            optional_fields = {
                field: updated_event[field] for field in OPTIONAL_EVENT_FIELDS if field in updated_event.keys()}
            calendar_service.create_events_in_windows(
                calendar_ids=self.calendar_lookup['calendar_id'].values,
                start_timestamp=split_timestamp,
                duration=end_timestamp - split_timestamp,
                target_event_summary=updated_event['summary'],
                target_calendar_id=target_calendar_id,
                feierabend=FEIERABEND,
                **optional_fields,
            )


api.add_resource(CalendarHandler, '/')

if __name__ == '__main__':
    app.run(debug=True)
