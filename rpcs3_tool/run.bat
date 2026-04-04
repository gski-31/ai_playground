@echo off
echo Installing dependencies...
py -m pip install -r requirements.txt -q
echo.
echo Starting RPCS3 Game Export Tool...
start http://localhost:5000
py app.py %*
pause
