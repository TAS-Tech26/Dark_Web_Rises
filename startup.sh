#!/bin/bash
export PYTHONPATH=/home/site/wwwroot:$PYTHONPATH
cd /home/site/wwwroot
exec python3 -c "import sys; sys.path.insert(0, '/home/site/wwwroot'); import uvicorn; uvicorn.run('game:app', host='0.0.0.0', port=8000)"