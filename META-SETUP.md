# Meta setup, step by step

What you need before starting: a Facebook account, an Instagram account switched to **Professional** (Business or Creator), and a phone with WhatsApp for testing.

## 1. Create the app

1. Go to developers.facebook.com, log in, and register as a developer if asked (accept the terms, confirm your phone).
2. **My Apps > Create app.** Name it, add a contact email.
3. When asked for use cases, pick **"Connect with customers through WhatsApp"** and **"Manage messaging and content on Instagram"**. Leave the business portfolio blank for testing, or pick yours if you have one.
4. Open **App settings > Basic** and copy the **App secret** into `.env` as `META_APP_SECRET`.

## 2. WhatsApp Cloud API (free test number)

1. In the app, open **WhatsApp > API Setup**. Meta creates a test WhatsApp Business Account and a **test phone number** for free.
2. Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID` and the **temporary access token** into `WHATSAPP_ACCESS_TOKEN` (it lasts 24 hours; for longer, create a System User token in Business Settings).
3. Under **"To"**, add your own WhatsApp number as a recipient and enter the code WhatsApp sends you. Up to 5 numbers can be added.
4. Press **Send message**. Meta sends its `hello_world` template to your phone. Reply to it from your phone: that opens the 24-hour window the bot needs.
5. Open **WhatsApp > Configuration**. Callback URL: `https://<public-url>/webhooks/whatsapp`. Verify token: the value of `META_VERIFY_TOKEN`. Press **Verify and save**, then **Manage** and subscribe to **messages**.

## 3. Instagram (Instagram API with Instagram Login)

1. In the Instagram app on your phone: **Settings > Account type and tools > Switch to professional account**. Make the account public if you want comment webhooks.
2. In the developer app, open **Instagram > API setup with Instagram login**.
3. Copy the **Instagram app secret** into `INSTAGRAM_APP_SECRET`.
4. **Generate access tokens > Add account**, log in to the Instagram account, and copy the token into `INSTAGRAM_ACCESS_TOKEN`. The account's ID shown there goes into `INSTAGRAM_ACCOUNT_ID`.
5. **Configure webhooks:** callback `https://<public-url>/webhooks/instagram`, same verify token, then subscribe to **comments** and **messages**.
6. In the Instagram app, **Settings > Messages and story replies > Message controls > Connected tools**, turn on **Allow access to messages**.

## 4. Facebook Lead Ads (optional)

1. In the developer app, add the use case **"Capture and manage ad leads with Marketing API"** (or add the permissions `leads_retrieval`, `pages_manage_metadata`, `pages_show_list`, `pages_read_engagement`, `pages_manage_ads` in the Graph API Explorer).
2. In the **Graph API Explorer**, pick the app, choose **Get Page Access Token** for the Page that owns the lead form, and copy it into `PAGE_ACCESS_TOKEN`.
3. Subscribe the app to the Page: `POST /{page-id}/subscribed_apps?subscribed_fields=leadgen` with that Page token. Like WhatsApp's `subscribed_apps`, the dashboard toggle alone is not enough.
4. In the app's **Webhooks** settings, pick the **Page** object, callback `https://<public-url>/webhooks/leads`, same verify token, and subscribe to **leadgen**.
5. Make a lead form on the Page (**Meta Business Suite > All tools > Instant Forms**; no ad or spend is needed), then open Meta's **Lead Ads Testing Tool** (developers.facebook.com/tools/lead-ads-testing), pick the Page and form, and press **Create lead**. The service fetches the answers and stores them in `data/leads.jsonl` and the `leads` table.

## 5. What works in test mode, and what needs App Review

| Works now, in development mode | Needs Meta App Review (takes days) |
|---|---|
| WhatsApp replies to up to 5 verified numbers via the test number | WhatsApp on a real business number for any customer (also needs business verification and a payment method) |
| Instagram DM replies to accounts that have a role on the app (admin, developer, tester) | Instagram DMs from the general public (Advanced Access to `instagram_business_manage_messages`) |
| Lead Ads test leads from Meta's Lead Ads Testing Tool, on a Page you admin | Leads from real ads on Pages you don't admin (Advanced Access to `leads_retrieval`) |
| Webhook verification and Meta's built-in **Test** button for each field | Comment webhooks at all (Advanced Access to `instagram_business_manage_comments`, app set to Live, public account) |
