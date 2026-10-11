#!/bin/bash

# Create / upgrade the database first. With IPTV_DB_PATH pointing at a mounted
# volume this is what sets up an empty volume on the very first start.
IPTV_SAFE_START=1 python3 manage.py migrate --noinput || exit 1

python3 manage.py runserver 0:8000 --noreload
