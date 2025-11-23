release: PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH} python qb_site/manage.py migrate --noinput
web: PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH} gunicorn qb_site.wsgi:application --bind 0.0.0.0:$PORT --log-file -
# Combined worker+beat on one dyno; uses two processes so both stay alive.
# Adjust concurrency/prefetch as needed.
worker: sh -c 'export PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH}; celery -A qb_site beat -l info --schedule /tmp/celerybeat-schedule & celery -A qb_site worker -l info -Q default --concurrency=2 --prefetch-multiplier=1 --max-tasks-per-child=200 --max-memory-per-child=250000 --without-gossip --without-mingle'
