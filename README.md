# GagoBolka
Gagodbolka es una herramienta en línea de comandos (CLI) ligera, rápida y portable diseñada para convertir archivos de texto gigante (.txt de 20.000 a 40.000+ palabras) en audio de alta calidad, combinando la velocidad de procesamiento local de Piper TTS con voces expresivas tipo audiolibro.
✨ Características Principales
⚡ Procesamiento Masivo y Rápido: Capaz de sintetizar archivos de texto extremadamente extensos en cuestión de minutos directamente desde la CPU (Depende de cada pc).

🎧 Calidad de Voz Superior: Configurado por defecto con modelos neuronales de alta definición como por ejemplo: (en_US-libritts_r-medium) para una entonación humana y natural, ideal para teorías, libros y documentos largos.

🎚️ Ritmo y Velocidad Ajustables: Implementa un factor de escala de tiempo (length_scale = 1.15) optimizado para una lectura pausada, clara y cómoda de escuchar.

💻 Entorno Interactivo y Portable: Diseñado con un bucle continuo que permite procesar múltiples archivos consecutivamente, con soporte para arrastrar y soltar archivos .txt directamente a la consola.

# 🛠️ Requisitos e Instalación
Clonar repositorio
git clone https://github.com/tu_usuario/Gagodbolka.git
cd Gagodbolka

Instalar dependencias
pip install piper-tts
