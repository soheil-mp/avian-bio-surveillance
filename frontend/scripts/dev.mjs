import { spawn } from 'child_process';

const shouldPrewarmExplore = process.env.DEV_PREWARM_EXPLORE === 'true';

const child = spawn('./node_modules/.bin/next', ['dev'], {
  cwd: process.cwd(),
  env: {
    ...process.env,
    WATCHPACK_POLLING: process.env.WATCHPACK_POLLING ?? 'true',
  },
  stdio: ['inherit', 'pipe', 'pipe'],
});

let localUrl = 'http://localhost:3000';
let readyLine = null;
let readyPrinted = false;
let prewarmStarted = false;

function forwardLine(line, stream) {
  stream.write(`${line}\n`);
}

function parseLocalUrl(line) {
  const match = line.match(/https?:\/\/[^\s]+/);
  if (match) {
    localUrl = match[0];
  }
}

async function fetchAndDrain(url) {
  const response = await fetch(url, {
    headers: {
      'x-explore-prewarm': '1',
    },
  });

  await response.arrayBuffer();
}

async function prewarmExplore() {
  if (!shouldPrewarmExplore) {
    if (readyLine && !readyPrinted) {
      forwardLine(readyLine, process.stdout);
      readyPrinted = true;
    }
    return;
  }

  if (prewarmStarted) {
    return;
  }

  prewarmStarted = true;
  const startedAt = Date.now();

  try {
    await fetchAndDrain(`${localUrl}/explore`);
    await fetchAndDrain(`${localUrl}/api/explore?warmup=1`);

    if (readyLine && !readyPrinted) {
      forwardLine(
        `${readyLine} [prewarmed /explore in ${((Date.now() - startedAt) / 1000).toFixed(1)}s]`,
        process.stdout,
      );
      readyPrinted = true;
    }
  } catch (error) {
    if (readyLine && !readyPrinted) {
      forwardLine(readyLine, process.stdout);
      readyPrinted = true;
    }

    const message = error instanceof Error ? error.message : String(error);
    forwardLine(`Explore prewarm failed: ${message}`, process.stderr);
  }
}

function handleLine(line, stream) {
  if (!line) {
    forwardLine(line, stream);
    return;
  }

  if (line.includes('- Local:')) {
    parseLocalUrl(line);
    forwardLine(line, stream);
    return;
  }

  if (line.includes('✓ Ready in')) {
    readyLine = line;
    void prewarmExplore();
    return;
  }

  forwardLine(line, stream);
}

function attachLineReader(stream, target) {
  let buffer = '';

  stream.setEncoding('utf8');
  stream.on('data', (chunk) => {
    buffer += chunk;

    while (true) {
      const newlineIndex = buffer.indexOf('\n');
      if (newlineIndex === -1) {
        break;
      }

      const line = buffer.slice(0, newlineIndex).replace(/\r$/, '');
      buffer = buffer.slice(newlineIndex + 1);
      handleLine(line, target);
    }
  });

  stream.on('end', () => {
    if (buffer.length > 0) {
      handleLine(buffer.replace(/\r$/, ''), target);
    }
  });
}

attachLineReader(child.stdout, process.stdout);
attachLineReader(child.stderr, process.stderr);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    child.kill(signal);
  });
}

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }

  process.exit(code ?? 0);
});