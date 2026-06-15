#!/bin/bash
# Quick Setup Script for Celery + Redis on Development Server
# Run as: bash quick_setup.sh

set -e

echo "=========================================="
echo "Celery + Redis Quick Setup"
echo "=========================================="
echo ""

# Check if running as django_user_1
if [ "$USER" != "django_user_1" ]; then
    echo "⚠️  Warning: This script is configured for user 'django_user_1'"
    echo "   Current user: $USER"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Install Redis if not installed
if ! command -v redis-cli &> /dev/null; then
    echo "Installing Redis..."
    sudo apt update
    sudo apt install redis-server -y
else
    echo "✓ Redis already installed"
fi

# Start Redis
echo "Starting Redis..."
sudo systemctl start redis-server
sudo systemctl enable redis-server

# Test Redis
if redis-cli ping | grep -q "PONG"; then
    echo "✓ Redis is running"
else
    echo "❌ Redis failed to start"
    exit 1
fi

# Navigate to project
cd ~/panamera_backend/panamera_backend

# Check if venv exists
if [ ! -d "../env" ]; then
    echo "❌ Virtual environment not found at ~/panamera_backend/env"
    exit 1
fi

echo "Activating virtual environment..."
source ../env/bin/activate

# Install Celery packages
echo "Installing Celery packages..."
pip install -q celery==5.3.4 redis==5.0.1 django-celery-beat==2.5.0

# Run migrations
echo "Running migrations..."
python manage.py migrate django_celery_beat --noinput

# Make shell scripts executable
cd ..
chmod +x start_celery_worker.sh start_celery_beat.sh

# Check deadline configuration
echo ""
echo "Checking certificate deadline configuration..."
DEADLINE=$(grep "LEAVE_CERTIFICATE_DEADLINE_HOURS" panamera_backend/api/constants.py | head -1)
echo "$DEADLINE"

if echo "$DEADLINE" | grep -q "1/60"; then
    echo ""
    echo "⚠️  WARNING: Deadline is set to 1 minute (TESTING MODE)"
    echo ""
    echo "To change to production (12 hours):"
    echo "  nano ~/panamera_backend/panamera_backend/api/constants.py"
    echo "  Change line 68 to: LEAVE_CERTIFICATE_DEADLINE_HOURS = 12"
    echo ""
fi

echo "=========================================="
echo "✅ Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Start Celery Worker (Terminal 1):"
echo "   cd ~/panamera_backend"
echo "   ./start_celery_worker.sh"
echo ""
echo "2. Start Django Server (Terminal 2):"
echo "   cd ~/panamera_backend/panamera_backend"
echo "   source ../env/bin/activate"
echo "   python manage.py runserver 0.0.0.0:8000"
echo ""
echo "3. Test Celery:"
echo "   python manage.py shell"
echo "   >>> from api.tasks import test_celery"
echo "   >>> result = test_celery.delay()"
echo "   >>> print(result.get(timeout=5))"
echo ""
echo "For full documentation, see: CELERY_SETUP.md"
echo ""
