# Sistema visual PATIA V1.1

Esta referencia define la dirección visual evaluada en Landing, Inicio e
Inventario antes de extenderla al resto de PATIA.

## Carácter del producto

PATIA es una herramienta para operar un negocio todos los días. Su interfaz
debe sentirse confiable, directa y tranquila: más cercana a un tablero de
operación que a una plantilla promocional de SaaS. Los datos y las acciones
dirigen; la decoración solamente ayuda.

## Base

- Fondo: neutro cálido `#F3F0EA`.
- Superficie principal: `#FFFEFB`.
- Superficie secundaria: `#ECE9E2`.
- Texto principal: `#1D252B`.
- Texto secundario: `#626D72`.
- Borde estructural: `#D6D2CA`.
- Acento PATIA: `#5147D9`, reservado para estado activo, enlaces, foco y
  acciones principales.
- Positivo: `#167159`; advertencia: `#A7601C`; crítico: `#B33F46`.

## Tipografía

El sistema usa la familia nativa del producto: Inter cuando está disponible,
seguida de Segoe UI y Arial. Los títulos operativos tienen tamaños contenidos y
capitalización natural. Las etiquetas evitan mayúsculas innecesarias. Los
importes y valores numéricos usan cifras tabulares.

## Forma y profundidad

- Escala de radios: 4, 7, 10 y 12 px.
- Los controles usan 7 px; los paneles, 10 px; únicamente las vistas principales
  del producto usan 12 px.
- Los paneles estándar tienen un borde de un píxel y no usan sombra.
- Las sombras se reservan para diálogos, menús flotantes y la demostración
  principal del producto en la Landing.

## Espaciado y densidad

La unidad base es 4 px. Los espacios frecuentes son 8, 12, 16, 24, 32 y 48 px.
Las pantallas operativas priorizan rellenos de 12 a 20 px. Las secciones
comerciales usan un ritmo vertical de 48 a 80 px en lugar de franjas vacías
sobredimensionadas.

## Componentes

- Botones: acento sólido para la única acción principal; borde neutro para las
  acciones secundarias; sin gradientes.
- Tablas: filas compactas, encabezados discretos, divisores horizontales,
  cifras tabulares y estado comunicado con indicador lateral más texto, no solo
  con color.
- Formularios: etiquetas visibles, controles de 42 a 44 px, foco claro y
  superficies planas.
- Métricas: alineadas sobre una misma base, con etiquetas pequeñas y valores
  destacados sin exceso.
- Iconos: una sola familia de Font Awesome, sin contenedor salvo cuando el icono
  comunica un estado.
- Gráficas: violeta, verde, ámbar, azul y rojo ladrillo; cuadrícula discreta y
  sin gradientes decorativos.

## Accesibilidad y movimiento

Cada elemento interactivo conserva un indicador de foco visible. Los objetivos
táctiles miden al menos 42 px. Las transiciones se limitan a 160 ms y se
desactivan con `prefers-reduced-motion`. Ningún estado depende solamente del
color.
