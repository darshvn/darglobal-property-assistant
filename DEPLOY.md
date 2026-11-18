# Getting a public URL

The assignment wants a URL someone can click and test. Three ways, best first.
All of them need the image built once:

```bash
docker build -t darglobal-bot .
```

## 1. Google Cloud Run — the one to use

Cold start is a couple of seconds, it scales to zero, and the free tier
(2M requests a month) will never be touched by a hiring review.

Already done: SDK installed, signed in as darshanpatil15105@gmail.com, project
`darglobal-bot-2026` created, region `asia-south1`.

Still needed, once only: **link a billing account to the project.** Cloud Run
will not enable its APIs without one, even to stay inside the free tier.

  <https://console.cloud.google.com/billing/linkedaccount?project=darglobal-bot-2026>

The existing billing account on this login is closed, so it needs reopening or
replacing with a new one.

Then:

```bash
./deploy.sh
```

That enables the APIs, puts the key in Secret Manager (so it is never baked
into the image or passed on a command line), deploys, prints the public URL
and health-checks it. Re-running it redeploys.

## 2. Render — no card needed, but it sleeps

Free web services spin down after 15 minutes idle and take about a minute to
wake. A reviewer who clicks the link cold waits through a loading screen,
which is why this is second choice. Push the repo to GitHub, then on
<https://dashboard.render.com> pick New → Web Service → the repo, runtime
Docker, and add `OPENROUTER_API_KEY` as an environment variable.

Wake it right before sending the email and it will be warm when they open it.

## 3. cloudflared — works this minute, no account at all

```bash
docker run -d --name darglobal-bot -p 7860:7860 \
  -e OPENROUTER_API_KEY="$OPENROUTER_API_KEY" darglobal-bot
cloudflared tunnel --url http://localhost:7860
```

It prints a `https://something.trycloudflare.com` URL that works immediately.
The catch: it lives only while this Mac is awake and the command is running,
so it is a demo link, not a submission link. Useful for checking the thing
works end to end, or for a live walkthrough on a call.

## Afterwards

Take the deployment down once they have reviewed it. It serves data scraped
from DarGlobal and from Wasalt, a partner company, and there is no reason for
it to stay up on the public internet after the assessment.
