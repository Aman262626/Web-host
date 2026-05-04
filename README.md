# Web-Host

Instant website hosting via **Telegram Bot** and **Web Dashboard**. Upload a ZIP file and get a live website link in seconds.

## Features

### Telegram Bot
- **ZIP Upload** - Send a ZIP file to deploy a website instantly
- **Site Management** - `/mysites`, `/delete`, `/rename` commands
- **Admin Panel** - `/stats`, `/broadcast`, `/allusers`, `/allsites`
- **Inline Buttons** - Interactive menu for easy navigation
- **Analytics** - Visit tracking for each deployed site
- **Rate Limiting** - Max 50MB per upload
- **Multi-User** - Each user manages their own sites

### Web Dashboard (Vercel)
- **Modern Dark/Light UI** - Responsive design with theme toggle
- **Upload Interface** - Drag & drop ZIP upload with progress
- **Site Management** - View, search, copy URL, delete sites
- **Dashboard** - Stats overview with total sites, visits, users
- **Settings** - Configure API URL and API key

### Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu with inline buttons |
| `/help` | Show all commands |
| `/mysites` | List your deployed sites |
| `/delete <name>` | Delete a site |
| `/rename <old> <new>` | Rename a site |
| `/stats` | View statistics |
| `/broadcast <msg>` | Send message to all users (admin) |
| `/allusers` | List all users (admin) |
| `/allsites` | List all sites (admin) |

## Setup

### 1. Bot Setup (Backend)

```bash
cd bot
pip install -r requirements.txt
```

Create a `.env` file:
```env
BOT_TOKEN=your_telegram_bot_token
DOMAIN=https://your-domain.com
ADMIN_ID=your_telegram_user_id
API_KEY=your_secret_api_key
```

Run:
```bash
python webhost.py
```

Get a public domain using Cloudflare Tunnel:
```bash
cloudflared tunnel --url http://localhost:8000
```

### 2. Web Dashboard (Vercel)

Deploy the `frontend/` directory to Vercel:

1. Go to [vercel.com](https://vercel.com)
2. Import the repository
3. Set **Root Directory** to `frontend`
4. Deploy

After deployment, open the dashboard and configure:
- **API URL**: Your bot backend URL (from cloudflare tunnel or Render)
- **API Key**: Same as `API_KEY` in your `.env`

### 3. Deploy Bot on Render (Optional)

1. Go to [render.com](https://render.com)
2. Create a new **Web Service**
3. Connect your GitHub repo
4. Set **Root Directory** to `bot`
5. Add environment variables (`BOT_TOKEN`, `DOMAIN`, `ADMIN_ID`, `API_KEY`)
6. Deploy

## API Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/` | Server status | No |
| GET | `/api/sites` | List all sites | No |
| GET | `/api/stats` | Server statistics | No |
| POST | `/api/upload` | Upload a site (multipart form) | API Key |
| DELETE | `/api/sites/<name>` | Delete a site | API Key |
| GET | `/<site>/` | Visit a hosted site | No |

## Project Structure

```
Web-host/
├── README.md
├── .gitignore
├── bot/                     # Telegram Bot + Flask Server
│   ├── webhost.py           # Main application
│   ├── requirements.txt     # Python dependencies
│   ├── Procfile             # Render deployment
│   ├── Dockerfile           # Docker deployment
│   ├── render.yaml          # Render config
│   └── .env.example         # Environment template
└── frontend/                # Web Dashboard (Vercel)
    ├── index.html           # Dashboard UI
    ├── vercel.json          # Vercel config
    ├── css/
    │   └── style.css        # Styles
    └── js/
        └── app.js           # Application logic
```

## Tech Stack

- **Bot**: Python, python-telegram-bot, Flask, Flask-CORS
- **Frontend**: HTML5, CSS3, JavaScript (vanilla)
- **Hosting**: Vercel (frontend), Render/VPS (bot)
- **Tunnel**: Cloudflare Tunnel (for local development)
