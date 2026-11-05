import type { Register } from 'claude-code'

// 把上下文和限额写到 ~/.claude/glassclaude/usage/<会话 ID>.json，眼镜的桥接程序来读。
// 限额只有 Claude Code 自己知道，本机别的地方读不到。这里只写这一个文件，不联网，不改对话。
export const register: Register = on => {
  on('session.measure', async ($, e, next) => {
    const result = await next(e)
    try {
      const home = await $.env.get('HOME')
      if (home !== undefined) {
        const session = await $.session.id()
        const at = await $.clock.now()
        const { tokens, window, percent } = e.context
        await $.fs.write(
          `${home}/.claude/glassclaude/usage/${session}.json`,
          JSON.stringify({ session, at, context: { tokens, window, percent }, rateLimits: e.rateLimits }),
        )
      }
    } catch {
      // 写不了只是眼镜上少几个数，不能影响会话
    }
    return result
  })
}
