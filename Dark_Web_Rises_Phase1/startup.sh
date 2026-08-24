#!/bin/bash
echo "=== CURRENT DIRECTORY ==="
pwd
echo "=== CONTENTS OF WWWROOT ==="
ls -la /home/site/wwwroot

echo "=== LOOKING FOR UVICORN ==="
find /home/site/wwwroot -name "uvicorn"

echo "=== STARTING APP ==="
export PYTHONPATH=/home/site/wwwroot:$PYTHONPATH
python3 -c "import sys; sys.path.insert(0, '/home/site/wwwroot'); import uvicorn; uvicorn.run('game:app', host='0.0.0.0', port=8000)"