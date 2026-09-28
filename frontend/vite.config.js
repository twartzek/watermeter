import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'child_process'

// Determine a build version identifier automatically, without any manual
// version bumping: short git commit hash (+ "-dirty" if there are uncommitted
// changes) and the build timestamp. Shown in the UI so it's easy to verify a
// deploy actually shipped the latest code (see components/VersionBadge.jsx).
function getGitDescribe() {
  try {
    const hash = execSync('git rev-parse --short HEAD').toString().trim()
    const dirty = execSync('git status --porcelain').toString().trim().length > 0
    return dirty ? `${hash}-dirty` : hash
  } catch {
    return 'unknown'
  }
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(getGitDescribe()),
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
})
