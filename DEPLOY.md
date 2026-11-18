# Getting a public URL

The assignment wants a URL someone can click and test. Three ways, best first.
All of them need the image built once:

```bash
docker build -t darglobal-bot .
```

## 1. Google Cloud Run — the one to use

Cold start is a couple of seconds, it scales to zero, and the free tier
(2M requests a month) will never be touched by a hiring review. A card is
required to open the account; the free tier does not charge.

```bash
brew install --cask google-cloud-sdk
gcloud init                                   # sign in, create a project
gcloud run deploy darglobal-bot \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --set-env-vars OPENROUTER_MODEL=inclusionai/ling-3.2-flash:free \
  --set-secrets OPENROUTER_API_KEY=openrouter-key:latest
```

Create the secret first:

```bash
gcloud secrets create openrouter-key --data-file=- <<< "$OPENROUTER_API_KEY"
```

Cloud Run gives back a `https://darglobal-bot-xxxx.a.run.app` URL. That is the
one to send.

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
