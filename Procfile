release: python qb_site/manage.py migrate --noinput
web: gunicorn qb_site.wsgi:application --bind 0.0.0.0:$PORT --log-file -
# Combined worker+beat on one dyno; uses two processes so both stay alive.
# Adjust concurrency/prefetch as needed.
worker: sh -c 'celery -A qb_site beat -l info --schedule /tmp/celerybeat-schedule & celery -A qb_site worker -l info -Q default --concurrency=2 --prefetch-multiplier=1'
