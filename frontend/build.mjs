import { copyFile, mkdir } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const frontendDirectory = dirname(fileURLToPath(import.meta.url))
const outputDirectory = join(frontendDirectory, 'dist')

await mkdir(outputDirectory, { recursive: true })
await build({
  entryPoints: [join(frontendDirectory, 'src', 'main.jsx')],
  bundle: true,
  minify: true,
  sourcemap: true,
  outdir: join(outputDirectory, 'assets'),
  entryNames: 'app',
})
await Promise.all([
  copyFile(join(frontendDirectory, 'index.html'), join(outputDirectory, 'index.html')),
  copyFile(join(frontendDirectory, 'logo.png'), join(outputDirectory, 'logo.png')),
])
