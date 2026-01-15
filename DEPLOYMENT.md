# Deployment Guide for Render

This guide will help you deploy the Hotel Booking Chatbot to Render.

## Prerequisites

1. A Render account (sign up at https://render.com)
2. Your repository pushed to GitHub/GitLab/Bitbucket
3. All required API keys and credentials

## Quick Deploy

### Option 1: Using render.yaml (Recommended)

1. Push your code to GitHub (make sure `render.yaml` is in the root)
2. Go to Render Dashboard → New → Blueprint
3. Connect your repository
4. Render will automatically detect `render.yaml` and create the service
5. Add your environment variables (see below)

### Option 2: Manual Setup

1. Go to Render Dashboard → New → Web Service
2. Connect your repository
3. Configure:
   - **Name**: `hotel-chatbot`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
   - **Health Check Path**: `/health`
4. Add environment variables (see below)

## Required Environment Variables

Add these in Render Dashboard → Your Service → Environment:

### Required
- `OPENAI_API_KEY` - Your OpenAI API key
- `WHATSAPP_ACCESS_TOKEN` - WhatsApp Business API access token
- `WHATSAPP_PHONE_NUMBER_ID` - Your WhatsApp phone number ID
- `WHATSAPP_VERIFY_TOKEN` - Token for webhook verification (create a secure random string)
- `GOOGLE_SHEET_ID` - Your Google Sheet ID (if using Google Sheets)
- `DASHBOARD_AUTH_TOKEN` - Secure token for dashboard access (create a secure random string)

### Optional
- `CREDENTIALS_JSON` - **Recommended**: Paste your entire credentials.json content here (easier than uploading files)
- `GOOGLE_SHEETS_CREDENTIALS_PATH` - Path to credentials.json file (default: `credentials.json`, only needed if not using CREDENTIALS_JSON)
- `CONTACT_PHONE_NUMBER` - Contact phone for group bookings (default: `975-17892899`)
- `MODEL_NAME` - OpenAI model (default: `gpt-4o-mini`)
- `DASHBOARD_PORT` - Dashboard port (default: `5001`)
- `MAX_ROOMS_PER_BOOKING` - Max rooms per booking (default: `3`)

## Google Sheets Credentials

You have two options:

### Option 1: Use CREDENTIALS_JSON Environment Variable (Recommended for Render)

1. Open your `credentials.json` file
2. Copy the **entire JSON content** (all of it, including braces)
3. In Render Dashboard → Your Service → Environment:
   - Add new environment variable: `CREDENTIALS_JSON`
   - Paste the entire JSON content as the value
   - Save
4. The service will automatically create `credentials.json` from this on startup
5. No need to upload any files!

**Example**: If your credentials.json looks like:
```json
{
  "type": "service_account",
  "project_id": "your-project",
  ...
}
```

Just paste the entire JSON (including the outer braces) into the `CREDENTIALS_JSON` environment variable.

### Option 2: Upload credentials.json File

1. Upload `credentials.json` to your Render service (via Shell or commit to repo)
2. Set `GOOGLE_SHEETS_CREDENTIALS_PATH` to the file path (default: `credentials.json`)

**Recommended**: Use Option 1 - it's easier and more secure on Render.

## Important Notes

1. **Port Configuration**: Render automatically sets the `PORT` environment variable. Your code already handles this.

2. **Health Check**: The service has a `/health` endpoint that Render uses to monitor the service.

3. **Dashboard Access**: The dashboard runs on a separate port (`DASHBOARD_PORT`). For production, consider:
   - Deploying dashboard as a separate service
   - Using a reverse proxy
   - Accessing via SSH tunnel

4. **Logs**: Check logs in Render Dashboard → Your Service → Logs

5. **Webhook URL**: After deployment, your webhook URL will be:
   ```
   https://your-service-name.onrender.com/webhook
   ```
   Use this URL in your WhatsApp Business API webhook configuration.

## Post-Deployment

1. **Configure WhatsApp Webhook**:
   - Go to Meta for Developers → Your App → WhatsApp → Configuration
   - Set Webhook URL: `https://your-service-name.onrender.com/webhook`
   - Set Verify Token: (same as `WHATSAPP_VERIFY_TOKEN`)
   - Subscribe to `messages` events

2. **Test the Service**:
   - Visit: `https://your-service-name.onrender.com/health`
   - Should return: `{"status": "healthy", ...}`

3. **Access Dashboard** (if needed):
   - The dashboard runs on port `DASHBOARD_PORT` (default: 5001)
   - For production access, consider deploying as a separate service

## Troubleshooting

### Service won't start
- Check logs in Render Dashboard
- Verify all required environment variables are set
- Ensure `credentials.json` is accessible if using Google Sheets

### Health check failing
- Verify `/health` endpoint is accessible
- Check that the service is binding to `0.0.0.0` (already configured)

### WhatsApp messages not working
- Verify webhook URL is correct in Meta dashboard
- Check `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`
- Review logs for API errors

## Security Best Practices

1. **Never commit**:
   - `.env` files
   - `credentials.json`
   - API keys
   - `sessions.json` or `bookings.json`

2. **Use Render Secrets**: Store sensitive data in Render's environment variables

3. **Dashboard Access**: Use a strong `DASHBOARD_AUTH_TOKEN`

4. **Rate Limiting**: Already configured in the code

## Scaling

For production workloads:
- Upgrade to `standard` or `pro` plan in `render.yaml`
- Consider using a database instead of JSON files for sessions/bookings
- Monitor API rate limits (OpenAI, WhatsApp, Google Sheets)
