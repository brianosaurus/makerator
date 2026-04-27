#!/bin/bash
# Deploy makerator to frankfurt and run with passed args.
# .env is shared with statalyzer via symlink (idempotent).
set -e

tar czf - --no-xattrs *.py requirements.txt | ssh frankfurt "mkdir -p makerator && cd makerator && tar xzf -"
ssh frankfurt "cd makerator; ln -sfn ../statalyzer/.env .env; source ~/venv/bin/activate; pip install -q -r requirements.txt && python3 makerator.py $*" | tee /tmp/makerator.log
