<#
.SYNOPSIS
Prepara una tarea diaria local. Solo la instala al indicar -Activar.
.DESCRIPTION
Usa pythonw del entorno virtual, sin ventana ni contraseñas en argumentos.
La cuenta debe tener sesión iniciada; el servidor Linux usa backup-cron.example.
La hora se interpreta en la zona horaria de Windows.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$Hora = '02:17',
    [ValidatePattern('^[A-Za-z0-9 _-]{1,80}$')]
    [string]$NombreTarea = 'PUNTO COM - respaldo diario',
    [switch]$Activar
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$proyecto = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Ruta-Proyecto([string]$Relativa, [switch]$DebeExistir) {
    $ruta = [IO.Path]::GetFullPath((Join-Path $proyecto $Relativa))
    $prefijo = $proyecto.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $ruta.StartsWith($prefijo, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'La ruta calculada sale de la carpeta del proyecto.'
    }
    # Evitar que un junction/enlace de una carpeta intermedia redirija el destino.
    $actual = $ruta
    while ($actual -ne $proyecto) {
        if (Test-Path -LiteralPath $actual) {
            $item = Get-Item -LiteralPath $actual -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Las rutas de la tarea no pueden contener enlaces o junctions.'
            }
        }
        $actual = Split-Path -Parent $actual
    }
    if ($DebeExistir -and -not (Test-Path -LiteralPath $ruta -PathType Leaf)) {
        throw "No existe el archivo requerido: $Relativa"
    }
    return $ruta
}

$python = Ruta-Proyecto 'venv\Scripts\python.exe' -DebeExistir
$pythonOculto = Ruta-Proyecto 'venv\Scripts\pythonw.exe' -DebeExistir
$archivoEntorno = Ruta-Proyecto '.env' -DebeExistir
$carpetaRespaldos = Ruta-Proyecto 'backups'
$null = Ruta-Proyecto 'deployment\respaldo_diario.py' -DebeExistir

Push-Location -LiteralPath $proyecto
try {
    & $python -m deployment.respaldo_diario --env-file $archivoEntorno --check
    if ($LASTEXITCODE -ne 0) { throw 'La comprobación de clientes PostgreSQL falló; no se programó la tarea.' }
} finally {
    Pop-Location
}

$identidad = [Security.Principal.WindowsIdentity]::GetCurrent()
$usuario = $identidad.Name
$argumentos = '-m deployment.respaldo_diario --env-file "{0}" --directory "{1}"' -f $archivoEntorno, $carpetaRespaldos
$plan = [pscustomobject]@{
    Tarea = $NombreTarea
    HoraLocal = $Hora
    ZonaHoraria = (Get-TimeZone).Id
    Cuenta = $usuario
    Ejecutable = $pythonOculto
    Argumentos = $argumentos
    Carpeta = $proyecto
    RequiereSesionIniciada = $true
    Estado = 'Vista previa; no se ha creado ninguna tarea'
}

if (-not $Activar) {
    $plan
    Write-Output 'Revisa el plan y ejecuta de nuevo con -Activar para registrar la tarea.'
    return
}

if ($usuario -match '(?i)\\CodexSandbox') {
    throw 'La cuenta actual es temporal de Codex. Instala la tarea desde PowerShell con tu cuenta Windows habitual.'
}

if (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue) {
    throw 'Ya existe una tarea con ese nombre; no se reemplazó. Revísala en el Programador de tareas.'
}

if ($PSCmdlet.ShouldProcess($NombreTarea, 'Registrar respaldo diario para la cuenta actual')) {
    $accion = New-ScheduledTaskAction -Execute $pythonOculto -Argument $argumentos -WorkingDirectory $proyecto
    $disparador = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($Hora, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
    $principal = New-ScheduledTaskPrincipal -UserId $identidad.User.Value -LogonType Interactive -RunLevel Limited
    $ajustes = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $NombreTarea -Action $accion -Trigger $disparador `
        -Principal $principal -Settings $ajustes -Description 'Copia y restaura en base temporal. Revisa backups\respaldos.log y el último resultado de la tarea.' | Out-Null
    $plan.Estado = 'Tarea registrada; todavía no se ha ejecutado un respaldo'
    $plan
}
