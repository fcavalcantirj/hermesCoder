echo "=== $(hostname) / $(whoami) ==="
echo "DISK:"; df -h / | tail -1
echo "MEM:"; free -m | awk '/^Mem:/{print $2" total, "$3" used, "$7" avail MB"}'
echo "LOAD:"; cat /proc/loadavg
echo "NPROC: $(nproc)"
echo "CURL: $(command -v curl || echo MISSING)"
echo "RWDIR:"; ls -ld "$HOME/resource-watch" 2>&1
echo "CRON(resource/thermal):"; crontab -l 2>/dev/null | grep -iE "resource|thermal" || echo "(none)"
echo "HERMES_ENV:"; ls -l "$HOME/.hermes/.env" 2>&1 | head -1
echo "NET(telegram):"; curl -s --max-time 8 -o /dev/null -w "%{http_code}\n" https://api.telegram.org 2>&1
