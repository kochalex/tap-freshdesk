#!/usr/bin/env python3

import collections
import functools
import sys
import time

import backoff
import requests
from requests.exceptions import HTTPError
import singer

from tap_freshdesk import utils


REQUIRED_CONFIG_KEYS = ['api_key', 'domain', 'start_date']
PER_PAGE = 100
BASE_URL = "https://{}.freshdesk.com"
CONFIG = {}
STATE = {}

endpoints = {
    "tickets": "/api/v2/tickets",
    "sub_ticket": "/api/v2/tickets/{id}/{entity}",
    "agents": "/api/v2/agents",
    "roles": "/api/v2/roles",
    "groups": "/api/v2/groups",
    "companies": "/api/v2/companies",
    "contacts": "/api/v2/contacts",
}

logger = singer.get_logger()
session = requests.Session()


def get_url(endpoint, **kwargs):
    return BASE_URL.format(CONFIG['domain']) + endpoints[endpoint].format(**kwargs)

@backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=5,
                      giveup=lambda e: e.response is not None and 400 <= e.response.status_code < 500,
                      factor=2)
@utils.ratelimit(1, 2)
def request(url, params=None):
    params = params or {}
    headers = {}
    if 'user_agent' in CONFIG:
        headers['User-Agent'] = CONFIG['user_agent']

    req = requests.Request('GET', url, params=params, auth=(CONFIG['api_key'], ""), headers=headers).prepare()
    logger.info("GET {}".format(req.url))
    resp = session.send(req)

    if 'Retry-After' in resp.headers:
        retry_after = int(resp.headers['Retry-After'])
        logger.info("Rate limit reached. Sleeping for {} seconds".format(retry_after))
        time.sleep(retry_after)
        return request(url, params)

    resp.raise_for_status()

    return resp


def get_start(entity):
    if entity not in STATE:
        STATE[entity] = CONFIG['start_date']

    return STATE[entity]


def gen_request(url, params=None):
    params = params or {}
    params["per_page"] = PER_PAGE
    page = STATE.get('page', 1)
    while True:
        params['page'] = page
        data = request(url, params).json()
        for row in data:
            yield row

        if len(data) == PER_PAGE:
            page += 1
            STATE['page'] = page
            singer.write_state(STATE)
        else:
            break

    STATE.pop('page', None)
    sincer.write_state(STATE)


def transform_dict(d, key_key="name", value_key="value"):
    return [{key_key: k, value_key: v} for k, v in d.items()]


def sync_tickets():
    singer.write_schema("tickets", utils.load_schema("tickets"), ["id"])
    singer.write_schema("conversations", utils.load_schema("conversations"), ["id"])
    singer.write_schema("satisfaction_ratings", utils.load_schema("satisfaction_ratings"), ["id"])
    singer.write_schema("time_entries", utils.load_schema("time_entries"), ["id"])

    start = get_start("tickets")
    params = {
        'updated_since': start,
        'order_by': "updated_at",
        'order_type': "asc",
    }
    last_updated = start

    for i, row in enumerate(gen_request(get_url("tickets"), params)):
        logger.info("Ticket {}: Syncing".format(row['id']))
        row.pop('attachments', None)
        row['custom_fields'] = transform_dict(row['custom_fields'])

        # get all sub-entities and save them
        logger.info("Ticket {}: Syncing conversations".format(row['id']))
        for subrow in gen_request(get_url("sub_ticket", id=row['id'], entity="conversations")):
            subrow.pop("attachments", None)
            subrow.pop("body", None)
            if subrow['updated_at'] >= start:
                singer.write_record("conversations", subrow)

        try:
            logger.info("Ticket {}: Syncing satisfaction ratings".format(row['id']))
            for subrow in gen_request(get_url("sub_ticket", id=row['id'], entity="satisfaction_ratings")):
                subrow['ratings'] = transform_dict(subrow['ratings'], key_key="question")
                if subrow['updated_at'] >= start:
                    singer.write_record("satisfaction_ratings", subrow)
        except HTTPError as e:
            if e.response.status_code == 403:
                logger.info("The Surveys feature is unavailable. Skipping the satisfaction_ratings stream.")
            else:
                raise

        try:
            logger.info("Ticket {}: Syncing time entries".format(row['id']))
            for subrow in gen_request(get_url("sub_ticket", id=row['id'], entity="time_entries")):
                if subrow['updated_at'] >= start:
                    singer.write_record("time_entries", subrow)

        except HTTPError as e:
            if e.response.status_code == 403:
                logger.info("The Timesheets feature is unavailable. Skipping the time_entries stream.")
            else:
                raise

        last_updated = max(row['updated_at'], last_updated)
        singer.write_record("tickets", row)

    utils.update_state(STATE, "tickets", last_updated)
    singer.write_state(STATE)


def sync_time_filtered(entity):
    singer.write_schema(entity, utils.load_schema(entity), ["id"])
    start = get_start(entity)

    logger.info("Syncing {} from {}".format(entity, start))
    for row in gen_request(get_url(entity)):
        if row['updated_at'] >= start:
            if 'custom_fields' in row:
                row['custom_fields'] = transform_dict(row['custom_fields'])

            utils.update_state(STATE, entity, row['updated_at'])
            singer.write_state(STATE)
            singer.write_record(entity, row)

    singer.write_state(STATE)


Stream = collections.namedtuple('Stream', ['name', 'sync'])
STREAMS = [
    Stream('tickets', sync_tickets),
    Stream('agents', functools.partial(sync_time_filtered, 'agents')),
    Stream('roles', functools.partial(sync_time_filtered, 'roles')),
    Stream('groups', functools.partial(sync_time_filtered, 'groups')),
    # commenting out this high-volume endpoint for now
    # Stream('contacts', functools.partial(sync_time_filtered, 'contacts')),
    Stream('companies', functools.partial(sync_time_filtered, 'companies')),
]


def do_sync():
    logger.info("Starting FreshDesk sync")

    for name, sync in STREAMS.items():
        if STATE.get('active_stream', name) != name:
            continue

        STATE['active_stream'] = name
        singer.write_state(STATE)

        sync()

        STATE.pop('active_stream', None)
        singer.write_state(STATE)

    logger.info("Completed sync")


def main():
    config, state = utils.parse_args(REQUIRED_CONFIG_KEYS)
    CONFIG.update(config)
    STATE.update(state)
    do_sync()


if __name__ == '__main__':
    main()
