#  CViwer

Visor ligero para ver tu capturadora (Xbox 360, PS3, Switch, etc.) **sin necesidad de OBS**, con muy poco consumo de recursos.

## Características

- Video directo de cualquier capturadora USB (DirectShow) con audio de baja latencia
- **Aceleración por GPU** (OpenGL): el escalado y los ajustes de color los hace la tarjeta gráfica, así que maximizar la ventana no baja los fps. Si la GPU no lo permite, cambia solo a un modo alternativo
- Todo en una sola ventana, con menú que se oculta/muestra con **C**
- Ajustes de color en vivo: brillo, contraste, gamma, saturación, tono, temperatura y volumen (en un panel desplegable)
- **Grabación** en MP4 (H.264 + audio), con codificación por hardware si está disponible (NVIDIA / Intel / AMD)
- Capturas de pantalla, pantalla completa, autodetección del audio de la capturadora
- Eliges tú la carpeta donde se guardan las grabaciones y las capturas (por defecto `Videos\CViwer` y `Imágenes\CViwer`)
- Recuerda tu configuración (se guarda en `%APPDATA%\CViwer`, sin ensuciar la carpeta del programa)

## Atajos

| Tecla | Acción |
|-------|--------|
| `C` | Ocultar / mostrar el menú |
| `F` / `F11` / doble clic | Pantalla completa |
| `Esc` | Salir de pantalla completa |
| `R` | Iniciar / detener grabación |
| `P` | Captura de pantalla |

## Descargar

Ve a la sección **[Releases](../../releases)** y descarga `CViwer.exe`. No requiere instalar nada.

## Ejecutar desde el código

Requiere Windows y Python 3.9+.

```bash
pip install -r requirements.txt
python cviwer.py
```

## Crear tu propio .exe

Ejecuta `crear_exe.bat`. El resultado queda en `dist/CViwer.exe`.

## Si no se escucha el audio

1. Pulsa **Actualizar dispositivos** y marca **Mostrar todos los dispositivos de audio**.
2. En Windows: Configuración → Sonido → Entrada (o `mmsys.cpl` → Grabación) y activa los dispositivos deshabilitados.
3. Permite el acceso al micrófono a las apps de escritorio en Configuración → Privacidad.

## Licencia

MIT, ver [LICENSE](LICENSE).
