param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\api\static')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null

function New-BechtholdIcon([int]$Size, [string]$Path) {
    $bitmap = [System.Drawing.Bitmap]::new($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.ColorTranslator]::FromHtml('#0c1420'))
        $graphics.TranslateTransform($Size / 2, $Size / 2)
        $graphics.RotateTransform(-12)
        $graphics.TranslateTransform(-$Size / 2, -$Size / 2)

        $tile = [single]($Size * 0.19)
        $gap = [single]($Size * 0.055)
        $step = $tile + $gap
        $start = ($Size - (2 * $tile + $gap)) / 2
        $colors = @('#5ee0b0', '#94e8cc', '#94e8cc', '#5ee0b0')
        for ($row = 0; $row -lt 2; $row++) {
            for ($column = 0; $column -lt 2; $column++) {
                $colorIndex = $row * 2 + $column
                $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($colors[$colorIndex]))
                try {
                    $x = [single]($start + $column * $step)
                    $y = [single]($start + $row * $step)
                    $graphics.FillRectangle($brush, $x, $y, $tile, $tile)
                }
                finally { $brush.Dispose() }
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

New-BechtholdIcon 180 (Join-Path $resolvedOutput 'apple-touch-icon.png')
New-BechtholdIcon 192 (Join-Path $resolvedOutput 'icon-192.png')
New-BechtholdIcon 512 (Join-Path $resolvedOutput 'icon-512.png')
Write-Output "Generated app icons in $resolvedOutput"
