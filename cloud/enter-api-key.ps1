param(
    [Parameter(Mandatory=$true)][string]$RequestPath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$requestData = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
$publicBytes = [Text.Encoding]::UTF8.GetBytes($requestData.public_key_pem)
$publicHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($publicBytes)).ToLowerInvariant()
if ($publicHash -ne $requestData.public_key_sha256) { throw 'Upload request fingerprint mismatch.' }
Write-Host "Run $($requestData.run_id): 88API / gpt-6.1-sol"
Write-Host 'Enter the key below. Input is hidden; the key is not saved as plain text.'
$secureKey = Read-Host '88API key' -AsSecureString
$rsa = [Security.Cryptography.RSA]::Create()
$plainBytes = $null
$plainKey = $null
try {
    $rsa.ImportFromPem($requestData.public_key_pem)
    $plainKey = [Net.NetworkCredential]::new('', $secureKey).Password
    $plainBytes = [Text.Encoding]::UTF8.GetBytes($plainKey)
    if ($plainBytes.Length -eq 0 -or $plainBytes.Length -gt 318 -or $plainKey -match '\s') {
        throw 'Invalid API key length or whitespace.'
    }
    $encrypted = $rsa.Encrypt($plainBytes,[Security.Cryptography.RSAEncryptionPadding]::OaepSHA256)
    $payload = @{
        run_id = [string]$requestData.run_id
        public_key_sha256 = $publicHash
        ciphertext = [Convert]::ToBase64String($encrypted)
    } | ConvertTo-Json
    [IO.File]::WriteAllText($OutputPath,$payload,[Text.UTF8Encoding]::new($false))
    Write-Host "Encrypted upload ready: $OutputPath"
} finally {
    if ($null -ne $plainBytes) { [Array]::Clear($plainBytes,0,$plainBytes.Length) }
    $plainKey = $null
    $secureKey.Dispose()
    $rsa.Dispose()
}
