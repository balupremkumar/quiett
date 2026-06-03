# Rebuilds the Vulkan-accelerated whisper.cpp toolchain and downloads the model.
# Run once after a clean checkout. Requires admin only if installing the prerequisites.
#
# Prerequisites (install manually if missing):
#   - Visual Studio 2022 Build Tools with C++ workload
#   - Vulkan SDK (https://vulkan.lunarg.com/sdk/home)
#   - Git
# CMake is installed via the project venv (no admin needed).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

# 1. Ensure CMake is in the venv
if (-not (Test-Path ".\venv\Scripts\cmake.exe")) {
    Write-Host "Installing cmake into venv..."
    & ".\venv\Scripts\pip.exe" install cmake
}

# 2. Clone whisper.cpp if missing
if (-not (Test-Path ".\third_party\whisper.cpp")) {
    Write-Host "Cloning whisper.cpp..."
    New-Item -ItemType Directory -Force -Path ".\third_party" | Out-Null
    git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git .\third_party\whisper.cpp
}

# 3. Configure + build with Vulkan
$vulkanSdk = $env:VULKAN_SDK
if (-not $vulkanSdk) {
    $candidate = Get-ChildItem "C:\VulkanSDK" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($candidate) { $vulkanSdk = $candidate.FullName }
}
if (-not $vulkanSdk) {
    throw "VULKAN_SDK not set and no install found under C:\VulkanSDK. Install the Vulkan SDK first."
}
$env:VULKAN_SDK = $vulkanSdk
Write-Host "Using Vulkan SDK: $vulkanSdk"

Set-Location ".\third_party\whisper.cpp"
if (-not (Test-Path ".\build\bin\Release\whisper-server.exe")) {
    & "..\..\venv\Scripts\cmake.exe" -B build -G "Visual Studio 17 2022" -A x64 `
        -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release `
        -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_SERVER=ON
    & "..\..\venv\Scripts\cmake.exe" --build build --config Release -j 8
}
Set-Location ..\..

# 4. Download the model if missing
$modelDir = ".\models"
$modelFile = "$modelDir\ggml-large-v3-turbo-q5_0.bin"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
if (-not (Test-Path $modelFile)) {
    Write-Host "Downloading large-v3-turbo Q5 model (~574 MB)..."
    $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
    Invoke-WebRequest -Uri $url -OutFile $modelFile -UseBasicParsing
}

Write-Host "`nSetup complete. whisper-server.exe + model are ready."
Write-Host "Launch the app with: .\venv\Scripts\python.exe main.py"
