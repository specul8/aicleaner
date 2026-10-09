// The languages the file editor knows (feature 80; spec 2026-10-07-f80): which file is which, how it
// is indented, its words and its snippets. PowerShell's commands come from this machine (the server
// compiles them, pscommands.py); the list here is only the fallback until that arrives.

const BY_EXTENSION = {
  ps1: 'powershell', psm1: 'powershell', psd1: 'powershell',
  tf: 'terraform', tfvars: 'terraform', hcl: 'terraform',
  py: 'python',
  json: 'json', jsonc: 'json', 'code-workspace': 'json',
  yml: 'yaml', yaml: 'yaml',
  md: 'markdown',
};

export function languageOf(name) {
  const match = /\.([A-Za-z0-9-]+)$/.exec(String(name || ''));
  return match ? BY_EXTENSION[match[1].toLowerCase()] || null : null;
}

// Section 5 of the spec, as decided: 4 spaces for PowerShell and Python, 2 for the rest.
export const INDENT = { powershell: '    ', python: '    ', terraform: '  ', json: '  ', yaml: '  ', markdown: '  ' };

const words = (text) => text.trim().split(/\s+/);

// The core modules, for completion until (or unless) the machine's own list arrives.
const CORE_CMDLETS = words(`
Add-Content Add-Member Add-Type Clear-Content Clear-Host Clear-Variable Compare-Object ConvertFrom-Csv
ConvertFrom-Json ConvertFrom-SecureString ConvertFrom-StringData ConvertTo-Csv ConvertTo-Html
ConvertTo-Json ConvertTo-SecureString ConvertTo-Xml Copy-Item Copy-ItemProperty Disable-PSRemoting
Enable-PSRemoting Enter-PSSession Exit-PSSession Export-Clixml Export-Csv Export-ModuleMember
ForEach-Object Format-List Format-Table Format-Wide Get-Acl Get-Alias Get-AuthenticodeSignature
Get-ChildItem Get-CimInstance Get-Command Get-Content Get-Credential Get-Date Get-Event Get-ExecutionPolicy
Get-FileHash Get-Help Get-History Get-Host Get-Item Get-ItemProperty Get-ItemPropertyValue Get-Job
Get-Location Get-Member Get-Module Get-NetAdapter Get-NetIPAddress Get-PSDrive Get-PSProvider
Get-PSSession Get-Process Get-Random Get-ScheduledTask Get-Service Get-TimeZone Get-Unique Get-Uptime
Get-Variable Get-WinEvent Group-Object Import-Clixml Import-Csv Import-Module Install-Module
Install-PackageProvider Invoke-Command Invoke-Expression Invoke-Item Invoke-RestMethod Invoke-WebRequest
Join-Path Measure-Command Measure-Object Move-Item New-Item New-ItemProperty New-Module New-Object
New-PSDrive New-PSSession New-ScheduledTask New-ScheduledTaskAction New-ScheduledTaskTrigger
New-Service New-TimeSpan New-Variable Out-File Out-GridView Out-Host Out-Null Out-String Pop-Location
Push-Location Read-Host Receive-Job Register-ScheduledTask Remove-Item Remove-ItemProperty Remove-Job
Remove-Module Remove-PSSession Remove-Variable Rename-Item Resolve-Path Restart-Computer Restart-Service
Save-Module Select-Object Select-String Select-Xml Set-Acl Set-Content Set-Date Set-ExecutionPolicy
Set-Item Set-ItemProperty Set-Location Set-PSRepository Set-Service Set-StrictMode Set-Variable
Sort-Object Split-Path Start-Job Start-Process Start-Service Start-Sleep Start-Transcript Stop-Job
Stop-Process Stop-Service Stop-Transcript Tee-Object Test-Connection Test-NetConnection Test-Path
Unblock-File Uninstall-Module Update-Module Wait-Job Wait-Process Where-Object Write-Debug Write-Error
Write-Host Write-Information Write-Output Write-Progress Write-Verbose Write-Warning`);

export const WORDS = {
  powershell: {
    keywords: words(`begin break catch class clean continue data default do dynamicparam else elseif end
      enum exit filter finally for foreach function hidden if in param process return static switch throw
      trap try until using while workflow`),
    operators: words(`-eq -ne -gt -ge -lt -le -like -notlike -match -notmatch -contains -notcontains -in -notin
      -replace -split -join -and -or -not -xor -is -isnot -as -band -bor -f -ceq -cne -clike -cmatch -imatch`),
    builtins: CORE_CMDLETS,
    variables: words(`$true $false $null $_ $PSItem $args $input $PSScriptRoot $PSCommandPath $MyInvocation
      $PSBoundParameters $PSVersionTable $ErrorActionPreference $VerbosePreference $LASTEXITCODE $env:`),
  },
  terraform: {
    keywords: words(`resource data module variable output locals provider terraform dynamic moved import check
      required_providers required_version backend lifecycle content for_each count depends_on source version
      provisioner connection precondition postcondition sensitive nullable default description type validation
      condition error_message ignore_changes create_before_destroy prevent_destroy replace_triggered_by
      true false null for in if`),
    builtins: words(`abs abspath alltrue anytrue base64decode base64encode basename can ceil chomp chunklist
      cidrhost cidrnetmask cidrsubnet cidrsubnets coalesce coalescelist compact concat contains csvdecode
      dirname distinct element endswith file filebase64 fileexists fileset filemd5 filesha256 flatten floor
      format formatdate formatlist indent index join jsondecode jsonencode keys length list log lookup lower
      map matchkeys max md5 merge min nonsensitive one parseint pathexpand plantimestamp pow range regex
      regexall replace reverse rsadecrypt sensitive setintersection setproduct setsubtract setunion sha1
      sha256 sha512 signum slice sort split startswith strcontains strrev substr sum templatefile textdecodebase64
      textencodebase64 timeadd timecmp timestamp title tobool tolist tomap tonumber toset tostring trim
      trimprefix trimspace trimsuffix try type upper urlencode uuid uuidv5 values yamldecode yamlencode zipmap
      var local each self path`),
  },
  python: {
    keywords: words(`False None True and as assert async await break class continue def del elif else except
      finally for from global if import in is lambda match case nonlocal not or pass raise return try while
      with yield self cls`),
    builtins: words(`abs aiter all anext any ascii bin bool breakpoint bytearray bytes callable chr classmethod
      compile complex delattr dict dir divmod enumerate eval exec filter float format frozenset getattr globals
      hasattr hash help hex id input int isinstance issubclass iter len list locals map max memoryview min next
      object oct open ord pow print property range repr reversed round set setattr slice sorted staticmethod
      str sum super tuple type vars zip __name__ __main__ __init__ Exception ValueError TypeError KeyError
      IndexError OSError RuntimeError NotImplementedError StopIteration`),
  },
  json: {
    // Keys of the files this project's people open most: VS Code workspaces and settings.
    keywords: words(`true false null`),
    builtins: words(`folders path name settings extensions recommendations launch tasks
      terminal.integrated.cwd editor.tabSize editor.formatOnSave files.exclude search.exclude
      $schema version`),
  },
  yaml: {
    // Azure DevOps pipelines, the YAML this project's people write most.
    keywords: words(`true false null yes no on off`),
    builtins: words(`trigger pr schedules resources pipelines repositories variables parameters stages stage
      jobs job deployment steps step pool vmImage name displayName dependsOn condition strategy matrix
      environment script pwsh powershell bash task inputs template extends checkout timeoutInMinutes
      continueOnError workingDirectory env group value type default values runOnce deploy`),
  },
  markdown: { keywords: [], builtins: [] },
};

// Snippets: `${n:placeholder}` stops reached by Tab in order, `$0` where the caret ends.
export const SNIPPETS = {
  powershell: [
    { label: 'function', body: 'function ${1:Verb-Noun} {\n    [CmdletBinding()]\n    param (\n        [Parameter(Mandatory)]\n        [string]$${2:Name}\n    )\n    $0\n}' },
    { label: 'param', body: 'param (\n    [Parameter(Mandatory)]\n    [string]$${1:Name}\n)' },
    { label: 'try', body: 'try {\n    ${1}\n}\ncatch {\n    Write-Error $_\n}' },
    { label: 'foreach', body: 'foreach ($${1:item} in $${2:items}) {\n    $0\n}' },
    { label: 'if', body: 'if (${1:condition}) {\n    $0\n}' },
    { label: 'switch', body: 'switch (${1:value}) {\n    ${2:match} { $0 }\n    default { }\n}' },
    { label: 'pscustomobject', body: '[pscustomobject]@{\n    ${1:Name} = ${2:value}\n}' },
  ],
  terraform: [
    { label: 'resource', body: 'resource "${1:type}" "${2:name}" {\n  $0\n}' },
    { label: 'data', body: 'data "${1:type}" "${2:name}" {\n  $0\n}' },
    { label: 'variable', body: 'variable "${1:name}" {\n  type        = ${2:string}\n  description = "${3}"\n}' },
    { label: 'output', body: 'output "${1:name}" {\n  value = ${2}\n}' },
    { label: 'module', body: 'module "${1:name}" {\n  source = "${2}"\n  $0\n}' },
    { label: 'locals', body: 'locals {\n  ${1:name} = ${2}\n}' },
    { label: 'dynamic', body: 'dynamic "${1:block}" {\n  for_each = ${2}\n  content {\n    $0\n  }\n}' },
  ],
  python: [
    { label: 'def', body: 'def ${1:name}(${2}):\n    $0' },
    { label: 'class', body: 'class ${1:Name}:\n    def __init__(self${2}):\n        $0' },
    { label: 'ifmain', body: 'if __name__ == "__main__":\n    ${1:main()}' },
    { label: 'try', body: 'try:\n    ${1}\nexcept ${2:Exception} as exc:\n    $0' },
    { label: 'with', body: 'with ${1:open(path)} as ${2:f}:\n    $0' },
  ],
  json: [],
  yaml: [
    { label: 'stage', body: '- stage: ${1:Build}\n  jobs:\n  - job: ${2:Job}\n    steps:\n    - ${0}' },
    { label: 'job', body: '- job: ${1:Job}\n  pool:\n    vmImage: ${2:windows-latest}\n  steps:\n  - ${0}' },
    { label: 'pwsh', body: '- pwsh: |\n    ${1}\n  displayName: ${2:Run}' },
    { label: 'task', body: '- task: ${1:Task@1}\n  inputs:\n    ${0}' },
  ],
  markdown: [
    { label: 'table', body: '| ${1:Column} | ${2:Column} |\n|---|---|\n| ${3} | ${4} |' },
    { label: 'code', body: '```${1:powershell}\n${0}\n```' },
    { label: 'link', body: '[${1:text}](${2:url})' },
  ],
};
