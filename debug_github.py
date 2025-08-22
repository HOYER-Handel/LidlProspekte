import os
import requests


def debug_github():
    token = os.getenv("GITHUB_TOKEN")
    owner = os.getenv("GITHUB_REPO_OWNER")
    repo = os.getenv("GITHUB_REPO_NAME")

    print(f"Checking GitHub repository: {owner}/{repo}")

    if not all([token, owner, repo]):
        print("❌ Missing GitHub configuration")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 1. Check if repository exists
    repo_url = f"https://api.github.com/repos/{owner}/{repo}"
    response = requests.get(repo_url, headers=headers)
    print(f"Repository check: {response.status_code}")
    if response.status_code != 200:
        print(f"❌ Repository not found: {response.text}")
        return False

    # 2. Check what branches exist
    branches_url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    response = requests.get(branches_url, headers=headers)
    print(f"Branches check: {response.status_code}")
    if response.status_code == 200:
        branches = response.json()
        print("Available branches:")
        for branch in branches:
            print(f"  - {branch['name']}")

    # 3. Check if workflow file exists in any branch
    print("\nChecking for workflow files...")
    for branch in ["main", "master", "feature/pythonanywhere-deplyoment"]:
        workflow_url = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows?ref={branch}"
        response = requests.get(workflow_url, headers=headers)
        if response.status_code == 200:
            files = response.json()
            print(f"📁 Workflows in {branch} branch:")
            for file in files:
                if file["type"] == "file":
                    print(f"  - {file['name']}")
            break
        else:
            print(f"❌ No workflows in {branch} branch: {response.status_code}")

    return True


if __name__ == "__main__":
    debug_github()
