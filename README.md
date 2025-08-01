# Prepzo User Portal

A comprehensive Flask-based web application for career development services, featuring AI-powered mock interviews, subscription management, and document handling.

## 🚀 Features

### Core Services
- **AI-Powered Mock Interviews**: Real-time voice interviews using LiveKit and OpenAI Realtime API
- **Subscription Management**: Stripe-integrated billing and plan management
- **Document Management**: Resume and job description upload with PDF processing
- **User Authentication**: Supabase Auth integration with RLS security

### Mock Interview System
- **Personalized Interviews**: AI agent loads user's resume and job description for contextual questioning
- **Multiple Interview Types**: Behavioral, Technical, Case Study, Leadership, and Sales interviews
- **Real-time Voice Interaction**: Low-latency voice communication using LiveKit
- **Smart Resume Processing**: Automatic PDF content extraction and analysis
- **Interview Analytics**: Comprehensive feedback and transcript generation

### Subscription Features
- **Stripe Integration**: Secure payment processing and customer portal
- **Feature Usage Tracking**: Plan-based limitations and usage monitoring
- **Webhook Processing**: Real-time subscription status updates
- **Billing History**: Complete transaction and invoice management

## 🏗️ Architecture

### Technology Stack
- **Backend**: Flask (Python 3.8+)
- **Database**: Supabase (PostgreSQL with RLS)
- **Authentication**: Supabase Auth
- **Payments**: Stripe
- **Real-time Communication**: LiveKit
- **AI/ML**: OpenAI Realtime API
- **File Storage**: Supabase Storage
- **Document Processing**: PyPDF2

### Project Structure
```
prepzo-user-portal/
├── app/
│   ├── __init__.py                 # Flask app factory
│   ├── extensions.py               # Third-party extensions
│   ├── main/                       # Main blueprint
│   ├── auth/                       # Authentication routes
│   └── userPortal/
│       ├── subscription/           # Billing & subscription management
│       ├── mockInterview/          # AI interview system
│       │   ├── agent.py           # LiveKit agent with AI
│       │   ├── api.py             # LiveKit API helpers
│       │   ├── function.py        # Resume loading tools
│       │   ├── prompt.py          # Interview prompts
│       │   └── routes.py          # Interview endpoints
│       └── documents/              # Document upload/management
├── run.py                          # Development server
├── run_all.py                      # Production server with agent
├── start_agent.py                  # Standalone agent launcher
├── requirements.txt                # Python dependencies
└── docker-compose.yml             # Container orchestration
```

## 🛠️ Setup & Installation

### Prerequisites
- Python 3.8+
- Node.js 16+ (for LiveKit agent dependencies)
- Supabase account
- Stripe account
- OpenAI API key
- LiveKit Cloud account

### Environment Variables
Create a `.env` file or configure the following in your environment:

```bash
# Supabase Configuration
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_anon_key
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key

# Stripe Configuration
STRIPE_SECRET_API_KEY=your_stripe_secret_key
STRIPE_WEBHOOK_SECRET=your_stripe_webhook_secret
STRIPE_PAID_PLAN_PRICE_ID_1=your_pro_plan_price_id
STRIPE_PAID_PLAN_PRICE_ID_2=your_premium_plan_price_id
STRIPE_SUCCESS_URL=http://localhost:3000/dashboard/settings/subscription
STRIPE_CANCEL_URL=http://localhost:3000/dashboard/settings/subscription

# LiveKit Configuration
LIVEKIT_URL=your_livekit_server_url
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

# OpenAI Configuration
OPENAI_API_KEY=your_openai_api_key

# Application Configuration
FRONTEND_ORIGIN=http://localhost:3000
FLASK_ENV=development
SECRET_KEY=your_secret_key
```

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd prepzo-user-portal
   ```

2. **Create virtual environment**
   ```bash
   python -m venv env
   source env/bin/activate  # On Windows: env\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up database**
   - Configure Supabase tables (see Database Schema section)
   - Enable Row Level Security (RLS)
   - Set up storage buckets

5. **Configure webhooks**
   - Set up Stripe webhooks pointing to `/userPortal/subscription/stripe/webhook`
   - Configure LiveKit webhooks for interview completion

## 🚀 Running the Application

### Development Mode
```bash
# Run Flask app only
python run.py

# Run Flask app + LiveKit agent (recommended)
python run_all.py
```

### Production Mode
```bash
# Set production environment
export USE_PRODUCTION=true

# Run with Gunicorn/Waitress + LiveKit agent
python run_all.py
```

### Docker Deployment
```bash
# Build and run with Docker Compose
docker-compose up --build

# Production deployment
docker-compose -f docker-compose.prod.yml up --build
```

## 📊 Database Schema

### Core Tables
- `user_subscriptions`: User subscription and billing information
- `subscription_plans`: Available subscription plans
- `subscription_histories`: Billing and subscription event logs
- `feature_usage`: User feature usage tracking
- `user_documents`: Uploaded resumes and job descriptions
- `mock_interview_sessions`: Interview session data and results

### Storage Buckets
- `user-documents`: PDF resumes, job descriptions, and other uploads

## 🔧 API Endpoints

### Subscription Management
- `GET /userPortal/subscription/status` - Get user subscription status
- `POST /userPortal/subscription/create-checkout-session` - Create Stripe checkout
- `POST /userPortal/subscription/customer-portal` - Access billing portal
- `POST /userPortal/subscription/stripe/webhook` - Stripe webhook handler

### Mock Interviews
- `POST /userPortal/mockInterview/create-session` - Create interview session
- `GET /userPortal/mockInterview/sessions` - List user sessions
- `POST /userPortal/mockInterview/webhook/interview-completed` - Interview completion handler
- `GET /userPortal/mockInterview/session/{id}/results` - Get interview results

### Document Management
- `POST /userPortal/documents/upload-document` - Upload resume/job description
- `GET /userPortal/documents/get-documents` - List user documents
- `DELETE /userPortal/documents/delete-document/{id}` - Delete document
- `PATCH /userPortal/documents/update-document-comments/{id}` - Update document

## 🤖 AI Interview Agent

### Features
- **Context-Aware**: Loads user's resume and job description before interview
- **Personalized Questions**: Asks specific questions based on user's background
- **Real-time Voice**: Low-latency conversation using OpenAI Realtime API
- **Multiple Types**: Supports behavioral, technical, case study interviews
- **Live Feedback**: Provides real-time encouragement and guidance

### Agent Tools
The interview agent includes several LiveKit function tools:
- `load_user_resume`: Fetches and processes user's resume from Supabase
- `load_job_description`: Loads target job description
- `prepare_interview_context`: Combines all context for personalized interviews

### Usage
```python
# The agent automatically loads user context when starting
# Room naming convention: "interview_{user_id}_{session_id}"
```

## 🔐 Security Features

### Authentication & Authorization
- Supabase Auth integration with JWT tokens
- Row Level Security (RLS) on all user data
- Admin client for system operations with explicit user filtering

### Data Protection
- Secure file uploads with type validation
- PDF content extraction with safety checks
- Webhook signature verification (Stripe)
- Input sanitization and validation

### Payment Security
- PCI-compliant Stripe integration
- Webhook signature validation
- Secure customer portal access

## 🧪 Testing

### Running Tests
```bash
# Install test dependencies
pip install pytest pytest-flask

# Run tests
pytest tests/

# Run with coverage
pytest --cov=app tests/
```

### Manual Testing
```bash
# Test subscription routes
python -c "from app.userPortal.subscription.routes import *"

# Test interview agent
python start_agent.py dev
```

## 📈 Monitoring & Logging

### Application Logging
- Structured logging with timestamps
- Separate log levels for development/production
- Error tracking for webhook failures and API issues

### Key Metrics
- Interview completion rates
- Subscription conversion tracking
- Document upload success rates
- Agent performance metrics

## 🚀 Deployment

### Environment Setup
1. **Production Environment Variables**: Configure all required environment variables
2. **Database Migration**: Ensure Supabase schema is up to date
3. **SSL Configuration**: Set up HTTPS for production
4. **Webhook URLs**: Update Stripe webhooks to production endpoints

### Docker Deployment
```bash
# Production build
docker build -t prepzo-user-portal .

# Run with environment file
docker run --env-file .env -p 5000:5000 prepzo-user-portal
```

### Health Checks
- `/health` - Application health status
- Database connectivity checks
- LiveKit agent status monitoring

## 🤝 Contributing

### Development Workflow
1. Fork the repository
2. Create a feature branch: `git checkout -b feature/new-feature`
3. Make changes and test thoroughly
4. Submit a pull request with detailed description

### Code Standards
- Follow PEP 8 for Python code
- Add docstrings for all functions and classes
- Include type hints where appropriate
- Write tests for new functionality

## 📄 License

[Add your license information here]

## 🆘 Support

For issues and questions:
1. Check the [Issues](https://github.com/your-org/prepzo-user-portal/issues) page
2. Review the documentation in `/docs`
3. Contact the development team

## 🔄 Version History

### v1.0.0 (Current)
- Initial release with mock interview system
- Stripe subscription integration
- Document management system
- LiveKit real-time communication
- AI-powered interview agent with resume processing

---

**Built with ❤️ for career development and interview preparation**