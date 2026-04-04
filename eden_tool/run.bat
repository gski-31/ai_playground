@echo off
echo Installing dependencies...
py -m pip install -r requirements.txt -q
echo.
echo Starting Eden Game Export Tool...
start http://localhost:5001
py app.py %*
pause
