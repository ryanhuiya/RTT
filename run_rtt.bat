@echo off
set MAP_FILE=build\Debug\R1_superstructure.map

echo Looking for RTT Control Block in %MAP_FILE%...

for /f "tokens=1" %%i in ('findstr "_SEGGER_RTT" %MAP_FILE%') do set RTT_ADDR=%%i

if "%RTT_ADDR%"=="" (
    echo Error: Could not find _SEGGER_RTT in map file!
    pause
    exit /b
)

echo Found RTT Address: %RTT_ADDR%
echo Starting pyOCD...

py -m pyocd rtt -t stm32g474retx -O connect_mode=attach -a %RTT_ADDR%