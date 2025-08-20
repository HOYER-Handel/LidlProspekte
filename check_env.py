# check_env.py - Save this in LidlProspekte folder (same level as manage.py)
import os


def check_github_env():
    print("Checking GitHub environment variables...")
    print("Current working directory:", os.getcwd())
    print()

    variables = [
        "USE_GITHUB_ACTIONS",
        "GITHUB_TOKEN",
        "GITHUB_REPO_OWNER",
        "GITHUB_REPO_NAME",
    ]

    for var in variables:
        value = os.getenv(var)
        if value:
            # Show only first few characters for tokens
            if var == "GITHUB_TOKEN" and value:
                print(f"✅ {var}: {value[:5]}... (hidden for security)")
            else:
                print(f"✅ {var}: {value}")
        else:
            print(f"❌ {var}: Not set")

    print("\n" + "=" * 50)
    print("If variables are not set, use these commands:")
    print("Windows CMD:")
    print("set GITHUB_TOKEN=your_github_token_here")
    print("set GITHUB_REPO_OWNER=your_github_username")
    print("set GITHUB_REPO_NAME=your_repository_name")
    print("set USE_GITHUB_ACTIONS=true")
    print()
    print("Windows PowerShell:")
    print('$env:GITHUB_TOKEN="your_github_token_here"')
    print('$env:GITHUB_REPO_OWNER="your_github_username"')
    print('$env:GITHUB_REPO_NAME="your_repository_name"')
    print('$env:USE_GITHUB_ACTIONS="true"')


if __name__ == "__main__":
    check_github_env()
