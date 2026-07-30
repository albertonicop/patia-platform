# SQLite temporal para pruebas locales

Los scripts que llaman `db.create_all()` deben utilizar una `DATABASE_URL`
explícita dentro del directorio temporal del sistema. La base persistente
`instance/tiendaia.db` nunca es un destino válido para pruebas, capturas o
datos demo.

Ejemplo seguro en PowerShell:

```powershell
$ErrorActionPreference = 'Stop'

$directory = Join-Path ([System.IO.Path]::GetTempPath()) (
    'patia-visual-' + [guid]::NewGuid().ToString('N')
)
[System.IO.Directory]::CreateDirectory($directory) | Out-Null

$databasePath = [System.IO.Path]::GetFullPath(
    (Join-Path $directory 'visual.db')
)
$env:DATABASE_URL = 'sqlite:///' + $databasePath.Replace('\', '/')

if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
    throw 'DATABASE_URL no quedó configurada.'
}

Write-Host "SQLite temporal resuelta: $databasePath"

@'
from app import create_app
from app.database_safety import assert_safe_ephemeral_database

app = create_app()
with app.app_context():
    resolved = assert_safe_ephemeral_database(app)
    print(f"Destino temporal aprobado: {resolved}")
'@ | python -

if ($LASTEXITCODE -ne 0) {
    throw 'La validación de la SQLite temporal falló.'
}
```

No debe utilizarse una sustitución regex como:

```powershell
$db -replace '\', '/'
```

`assert_safe_ephemeral_database()` también es ejecutado automáticamente antes
de cualquier llamada explícita a `db.create_all()`. Esta protección no se
ejecuta durante el arranque normal ni durante migraciones Alembic.
