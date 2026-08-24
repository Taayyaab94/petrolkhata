# Email and sign-in setup (for the pump owner)

This page is written for the owner running the app, not a programmer. No
code changes are needed for anything below - it's all done through the
Vercel website.

## 1. What already works with nothing set up

Right now, no email account is connected to the app. That's fine - it
does not stop you from using it:

- **Inviting staff** works today. When you invite someone from Settings,
  the app shows you an **invite link** right there on the page (with a
  Copy button). Send that link to the person yourself - WhatsApp, SMS,
  whatever you'd normally use. They open it, choose their own password,
  and they're in.
- **Verification emails** (the "please confirm your email" link) are
  **not** delivered while nothing is set up. This does not block you or
  your staff from using the app - it's just a "nice to have" that isn't
  active yet.

If you never do anything in the sections below, the app keeps working
exactly like this. Turning on email is optional.

## 2. Turning email on

This makes invite and verification emails actually arrive in someone's
inbox, instead of only showing up as a link on the Settings page.

1. Go to **resend.com** and create a free account.
2. Inside Resend, create an **API key** (look for "API Keys" in their
   left-hand menu). Copy the key it gives you - you'll only be shown it
   once.
3. Go to your Vercel project, then **Settings -> Environment Variables**.
4. Add a new variable named `RESEND_API_KEY` and paste the key as its
   value. Save.
5. Redeploy the app (Vercel usually prompts you to do this after saving
   an environment variable - if not, go to the Deployments tab and
   redeploy the latest one).

**Important limitation to check for yourself:** until you verify your
own sending domain at Resend (see step 3 below), Resend's free sender
address (`onboarding@resend.dev`) can generally only deliver mail to the
*same email address you signed up to Resend with* - not to your staff's
inboxes. Please **confirm this on your own Resend dashboard**, since
Resend's exact rules can change. Practically, this means: even after
adding `RESEND_API_KEY`, you may still need to use the copy-the-link
method for inviting staff, until you complete step 3.

## 3. Once you have your own domain

If your business has its own website domain (e.g. `yourpump.com`):

1. In Resend, go to **Domains** and add/verify that domain (Resend will
   give you some DNS records to add - if you don't manage your own DNS,
   whoever set up your website/domain can add these for you).
2. Once Resend shows the domain as verified, go back to Vercel ->
   Settings -> Environment Variables and add `MAIL_FROM` set to an
   address on that domain, e.g. `no-reply@yourpump.com`.
3. Redeploy.

After this, invite and verification emails should reliably reach any
address, not just your own.

## 4. `SECRET_KEY`

This one variable should already be set, but it's worth checking: in
Vercel -> Settings -> Environment Variables, there should be a
`SECRET_KEY` value. It needs to stay the same across every deploy -
sign-in sessions (and "Continue with Google", if you use it) depend on
it not changing. If it's missing, generate a long random value once and
save it as `SECRET_KEY` - then never change it.

## 5. Google sign-in (placeholder)

The app already has the code to support "Continue with Google" as an
alternative way to log in, controlled by a `GOOGLE_CLIENT_ID` (and a
matching `GOOGLE_CLIENT_SECRET`) environment variable in Vercel. If you
want this turned on, that's a separate setup step in Google's own
developer console - ask whoever manages your Google Workspace/Cloud
account to help create those credentials, then add both values the same
way as `RESEND_API_KEY` above. Leaving these unset is completely fine -
the app works normally with plain username/password sign-in either way.
