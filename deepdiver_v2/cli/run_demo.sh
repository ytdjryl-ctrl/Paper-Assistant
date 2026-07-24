#!/bin/bash

# DeepDiver Multi-Agent System CLI Demo Runner
# This script makes it easier to run the CLI demo with different options

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Function to show help
show_help() {
    echo "DeepDiver Multi-Agent System CLI Demo Runner"
    echo ""
    echo "Usage: $0 [OPTIONS] [QUERY]"
    echo ""
    echo "Options:"
    echo "  -h, --help              Show this help message"
    echo "  -i, --interactive       Start interactive mode (default)"
    echo "  -c, --config-only       Show configuration and exit"
    echo "  -e, --create-env        Create sample .env file from template"
    echo "  -q, --query \"QUERY\"     Execute a specific query"
    echo "  -d, --debug             Enable debug mode with verbose logging"
    echo "  --quiet                 Suppress all non-essential output"
    echo "  --setup                 Install dependencies and setup"
    echo ""
    echo "Examples:"
    echo "  $0 --interactive"
    echo "  $0 --query \"Research the latest trends in AI\""
    echo "  $0 --config-only"
    echo "  $0 --debug --query \"Debug a specific query\""
    echo "  $0 --quiet --query \"Run quietly\""
    echo "  $0 --setup"
    echo ""
}

# Function to setup the demo
setup_demo() {
    print_status "Setting up DeepDiver CLI Demo..."
    
    # Check if we're in the right directory
    if [ ! -f "$PROJECT_ROOT/cli/demo.py" ]; then
        print_error "Cannot find demo.py. Please run this script from the CLI directory or project root."
        exit 1
    fi
    
    # Install dependencies
    print_status "Installing Python dependencies..."
    cd "$PROJECT_ROOT"
    
    if [ -f "cli/requirements.txt" ]; then
        pip install -r cli/requirements.txt
        print_status "Dependencies installed successfully"
    else
        print_warning "requirements.txt not found, skipping dependency installation"
    fi
    
    # Check for .env file
    if [ ! -f "config/.env" ]; then
        print_warning "No .env file found in config/ directory"
        print_status "Creating sample .env file from template..."
        
        if [ -f "env.template" ]; then
            cp env.template config/.env
            print_status "Sample .env file created at config/.env"
            print_warning "Please edit config/.env with your actual configuration values"
        else
            print_error "No env.template found. Please create config/.env manually"
        fi
    else
        print_status ".env file found at config/.env"
    fi
    
    # Make demo script executable
    chmod +x "$PROJECT_ROOT/cli/demo.py"
    print_status "Made demo.py executable"
    
    print_status "Setup complete! You can now run the demo with:"
    echo "  $0 --interactive"
}

# Function to run the demo
run_demo() {
    local args=("$@")
    
    # Change to project root
    cd "$PROJECT_ROOT"
    
    print_status "Starting DeepDiver CLI Demo..."
    python cli/demo.py "${args[@]}"
}

# Parse command line arguments
DEMO_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        --setup)
            setup_demo
            exit 0
            ;;
        -c|--config-only)
            DEMO_ARGS+=("--config-only")
            shift
            ;;
        -e|--create-env)
            DEMO_ARGS+=("--create-env")
            shift
            ;;
        -q|--query)
            if [ -z "${2:-}" ]; then
                print_error "Query argument is required with --query option"
                show_help
                exit 1
            fi
            DEMO_ARGS+=("--query" "$2")
            shift 2
            ;;
        -d|--debug)
            DEMO_ARGS+=("--debug")
            shift
            ;;
        --quiet)
            DEMO_ARGS+=("--quiet")
            shift
            ;;
        -i|--interactive)
            # Interactive is default, no need to add args
            shift
            ;;
        *)
            # If it's not a flag, treat it as a query
            if [[ "$1" != -* ]]; then
                DEMO_ARGS+=("--query" "$1")
                shift
            else
                print_error "Unknown option: $1"
                show_help
                exit 1
            fi
            ;;
    esac
done

# Run the demo with collected arguments
run_demo "${DEMO_ARGS[@]}"
