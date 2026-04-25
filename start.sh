#!/bin/bash
# ─────────────────────────────────────────────
#  TripSplit — start script
#  Usage:
#    ./start.sh                  (local only)
#    ./start.sh --share          (start + open tunnel for friends)
#    ./start.sh --flights        (prompts for SerpAPI key)
#    ./start.sh --share --flights
# ─────────────────────────────────────────────

cd "$(dirname "$0")"

SHARE=false
for arg in "$@"; do
  [[ "$arg" == "--share" ]]   && SHARE=true
  [[ "$arg" == "--flights" ]] && WANT_FLIGHTS=true
done

# Prompt for SerpAPI key if --flights and not already set
if [[ "$WANT_FLIGHTS" == "true" && -z "$SERPAPI_KEY" ]]; then
  echo ""
  echo "✈️  SerpAPI flight search setup"
  echo "   Sign up free at: https://serpapi.com (250 searches/month, no credit card)"
  echo "   Dashboard → API Key → copy it"
  echo ""
  read -p "   SerpAPI Key: " SERPAPI_KEY
  export SERPAPI_KEY
fi

# Kill any old server on port 3000
lsof -ti:3000 | xargs kill -9 2>/dev/null
sleep 0.5

echo ""
echo "🌍 Starting TripSplit..."
python3 server.py &
SERVER_PID=$!
sleep 1.5

# Open browser locally
open http://localhost:3000 2>/dev/null || true

if [[ "$SHARE" == "true" ]]; then
  echo ""
  echo "🔗 Starting tunnel... (grabbing your link)"
  echo ""

  # Run cloudflared, watch its output for the URL, print it loudly
  ./cloudflared tunnel --url http://localhost:3000 --no-autoupdate 2>&1 | while IFS= read -r line; do
    if [[ "$line" == *"trycloudflare.com"* ]]; then
      URL=$(echo "$line" | grep -o 'https://[^ |]*trycloudflare\.com')
      echo ""
      echo "┌─────────────────────────────────────────────────┐"
      echo "│  ✅ SHARE THIS LINK WITH YOUR GROUP CHAT:       │"
      echo "│                                                 │"
      echo "│  $URL"
      echo "│                                                 │"
      echo "│  Press Ctrl+C to stop the app.                 │"
      echo "└─────────────────────────────────────────────────┘"
      echo ""
    fi
  done &
  TUNNEL_PID=$!

  wait $SERVER_PID
  kill $TUNNEL_PID 2>/dev/null
else
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  App running at: http://localhost:3000"
  echo ""
  echo "  To share with friends, stop this and run:"
  echo "    ./start.sh --share"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "  Press Ctrl+C to stop."
  echo ""
  wait $SERVER_PID
fi
