@echo off
setlocal
:: Always resolve files relative to this launcher, even when started from a shortcut.
cd /d "%~dp0"
title AH Sniper - Server + Tunnel
echo ============================================
echo   AH Sniper: EXE + Cloudflare Tunnel
echo   Public site: configured in local .env / PUBLIC_ORIGIN
echo ============================================
echo.

:: Токен туннеля — в tunnel_token.txt (gitignored, в репо НЕ попадает!)
set "TOK="
if exist tunnel_token.txt (
    set /p TOK=<tunnel_token.txt
) else (
    echo   [!] tunnel_token.txt не найден — туннель не запустится.
)

:: Start Cloudflare Tunnel (background)
:: --protocol http2: QUIC/UDP нестабилен на этой сети (502/530), http2 надёжнее
:: Optional local adapter selection keeps a VPN route from intercepting the tunnel.
:: Resolve the current address on each launch so DHCP changes do not break it.
if not defined TUNNEL_EDGE_BIND_ADDRESS if exist tunnel_interface.txt (
    for /f "delims=" %%I in ('powershell.exe -NoProfile -File "%~dp0tools\tunnel_bind_address.ps1"') do set "TUNNEL_EDGE_BIND_ADDRESS=%%I"
)
if defined TOK (
    powershell.exe -NoProfile -Command "if (Get-Process -Name cloudflared -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
    if errorlevel 1 (
        echo [1/2] Starting Cloudflare Tunnel...
        start "" /B cloudflared tunnel --protocol http2 run --token "%TOK%"
    ) else (
        echo [1/2] Cloudflare Tunnel already running
    )
) else (
    echo [1/2] Tunnel SKIPPED
)

:: Wait ~2 seconds for the tunnel to come up (ping works even with redirected stdin)
ping -n 3 127.0.0.1 >nul

:: Start EXE (admin build: push в Supabase + web-auth для PUBLIC_ORIGIN)
echo [2/2] Starting AH Sniper (admin)...
powershell.exe -NoProfile -Command "if (Get-Process -Name AuctionMonitorAdmin -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "" "%~dp0AuctionMonitorAdmin.exe"
) else (
    echo       AH Sniper is already running
)

echo.
echo   Public site: configured in local .env / PUBLIC_ORIGIN
echo   EXE:  localhost:8765
echo.
echo   Close this window to stop the tunnel.
echo ============================================
pause
endlocal
