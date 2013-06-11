import calendar
from datetime import datetime, timedelta
import json
import logging
import re
import rfc822

from django.conf import settings
from django.db.utils import IntegrityError

import cronjobs
from multidb.pinning import pin_this_thread
from statsd import statsd
from twython import Twython

from kitsune.customercare.models import Tweet, Reply
from kitsune.sumo.redis_utils import redis_client, RedisError


LINK_REGEX = re.compile('https?\:', re.IGNORECASE)
MENTION_REGEX = re.compile('(^|\W)@')
RT_REGEX = re.compile('^rt\W', re.IGNORECASE)

ALLOWED_USERS = [
    {'id': 2142731, 'username': 'Firefox'},
    {'id': 150793437, 'username': 'FirefoxBrasil'},
]

log = logging.getLogger('k.twitter')


@cronjobs.register
def collect_tweets():
    # Don't (ab)use the twitter API from dev and stage.
    if settings.STAGE:
        return

    """Collect new tweets about Firefox."""
    with statsd.timer('customercare.tweets.time_elapsed'):
        t = Twython(settings.TWITTER_CONSUMER_KEY,
                    settings.TWITTER_CONSUMER_SECRET,
                    settings.TWITTER_ACCESS_TOKEN,
                    settings.TWITTER_ACCESS_TOKEN_SECRET)

        search_options = {
            'q': 'firefox OR #fxinput OR @firefoxbrasil OR #firefoxos',
            'count': settings.CC_TWEETS_PERPAGE,  # Items per page.
            'result_type': 'recent',  # Retrieve tweets by date.
        }

        # If we already have some tweets, collect nothing older than what we
        # have.
        try:
            latest_tweet = Tweet.latest()
        except Tweet.DoesNotExist:
            log.debug('No existing tweets. Retrieving %d tweets from search.' %
                      settings.CC_TWEETS_PERPAGE)
        else:
            search_options['since_id'] = latest_tweet.tweet_id
            log.info('Retrieving tweets with id >= %s' % latest_tweet.tweet_id)

        # Retrieve Tweets
        results = t.search(**search_options)

        if len(results['statuses']) == 0:
            # Twitter returned 0 results.
            return

        # Drop tweets into DB
        for item in results['statuses']:
            # Apply filters to tweet before saving
            # Allow links in #fxinput tweets
            statsd.incr('customercare.tweet.collected')
            item = _filter_tweet(item, allow_links='#fxinput' in item['text'])
            if not item:
                continue

            created_date = datetime.utcfromtimestamp(calendar.timegm(
                rfc822.parsedate(item['created_at'])))

            item_lang = item['metadata'].get('iso_language_code', 'en')

            tweet = Tweet(tweet_id=item['id'], raw_json=json.dumps(item),
                          locale=item_lang, created=created_date)
            try:
                tweet.save()
                statsd.incr('customercare.tweet.saved')
            except IntegrityError:
                pass


@cronjobs.register
def purge_tweets():
    """Periodically purge old tweets for each locale.

    This does a lot of DELETEs on master, so it shouldn't run too frequently.
    Probably once every hour or more.

    """
    # Pin to master
    pin_this_thread()

    # Build list of tweets to delete, by id.
    for locale in settings.SUMO_LANGUAGES:
        locale = settings.LOCALES[locale].iso639_1
        # Some locales don't have an iso639_1 code, too bad for them.
        if not locale:
            continue
        oldest = _get_oldest_tweet(locale, settings.CC_MAX_TWEETS)
        if oldest:
            log.debug('Truncating tweet list: Removing tweets older than %s, '
                      'for [%s].' % (oldest.created, locale))
            Tweet.objects.filter(locale=locale,
                                 created__lte=oldest.created).delete()


def _get_oldest_tweet(locale, n=0):
    """Returns the nth oldest tweet per locale, defaults to newest."""
    try:
        return Tweet.objects.filter(locale=locale).order_by(
            '-created')[n]
    except IndexError:
        return None


def _filter_tweet(item, allow_links=False):
    """
    Apply some filters to an incoming tweet.

    May modify tweet. If None is returned, tweet will be discarded.
    Used to exclude replies and such from incoming tweets.
    """
    text = item['text'].lower()
    # No replies, except to ALLOWED_USERS
    to_user_id = item.get('to_user_id')
    if to_user_id and to_user_id not in [u['id'] for u in ALLOWED_USERS]:
        statsd.incr('customercare.tweet.rejected.reply_or_mention')
        return None

    # No mentions, except of ALLOWED_USERS. Let's remove
    # these from the text before checking for mentions.
    # Note: This has some edge cases like @firefoxrocks that will pass by.
    filtered_text = text
    for username in [u['username'].lower() for u in ALLOWED_USERS]:
        filtered_text = filtered_text.replace('@%s' % username, '')
    if MENTION_REGEX.search(filtered_text):
        statsd.incr('customercare.tweet.rejected.reply_or_mention')
        return None

    # No retweets
    if RT_REGEX.search(text) or text.find('(via ') > -1:
        statsd.incr('customercare.tweet.rejected.retweet')
        return None

    # No links
    if not allow_links and LINK_REGEX.search(text):
        statsd.incr('customercare.tweet.rejected.link')
        return None

    # Exclude filtered users
    if item['user']['screen_name'] in settings.CC_IGNORE_USERS:
        statsd.incr('customercare.tweet.rejected.user')
        return None

    return item


@cronjobs.register
def get_customercare_stats():
    """
    Generate customer care stats from the Replies table.

    This gets cached in Redis as a sorted list of contributors, stored as JSON.

    Example Top Contributor data:

    [
        {
            'twitter_username': 'username1',
            'avatar': 'http://twitter.com/path/to/the/avatar.png',
            'avatar_https': 'https://twitter.com/path/to/the/avatar.png',
            'all': 5211,
            '1m': 230,
            '1w': 33,
            '1d': 3,
        },
        { ... },
        { ... },
    ]
    """

    contributor_stats = {}

    now = datetime.now()
    one_month_ago = now - timedelta(days=30)
    one_week_ago = now - timedelta(days=7)
    yesterday = now - timedelta(days=1)

    for reply in Reply.objects.all():
        raw = json.loads(reply.raw_json)
        user = reply.twitter_username
        if user not in contributor_stats:
            if 'from_user' in raw: #For tweets collected using v1 API
                user_data = raw
            else:
                user_data = raw['user']

            contributor_stats[user] = {
                'twitter_username': user,
                'avatar': user_data['profile_image_url'],
                'avatar_https': user_data['profile_image_url_https'],
                'all': 0, '1m': 0, '1w': 0, '1d': 0,
            }
        contributor = contributor_stats[reply.twitter_username]

        contributor['all'] += 1
        if reply.created > one_month_ago:
            contributor['1m'] += 1
            if reply.created > one_week_ago:
                contributor['1w'] += 1
                if reply.created > yesterday:
                    contributor['1d'] += 1

    sort_key = settings.CC_TOP_CONTRIB_SORT
    limit = settings.CC_TOP_CONTRIB_LIMIT
    # Sort by whatever is in settings, break ties with 'all'
    contributor_stats = sorted(contributor_stats.values(),
        key=lambda c: (c[sort_key], c['all']), reverse=True)[:limit]

    try:
        redis = redis_client(name='default')
        key = settings.CC_TOP_CONTRIB_CACHE_KEY
        redis.set(key, json.dumps(contributor_stats))
    except RedisError as e:
        statsd.incr('redis.error')
        log.error('Redis error: %s' % e)

    return contributor_stats