import { ChildProcessWithoutNullStreams, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { createServer, IncomingMessage, ServerResponse } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import path from 'node:path';

const webDir = process.cwd();
const repoRoot = path.resolve(webDir, '..');
const pythonBinary = process.env.PYTHON || 'python';
const pythonMain = path.join(repoRoot, 'main.py');
const publicIndexPath = path.join(webDir, 'public', 'index.html');

let port = Number(process.env.PORT || 3000);
const MAX_BODY_BYTES = 1024 * 1024;
const MAX_ACTIVE_SESSIONS = 2;

async function findAvailablePort(startPort: number): Promise<number> {
    for (let p = startPort; p < startPort + 100; p++) {
        const server = createNetServer();
        try {
            await new Promise<void>((resolve, reject) => {
                server.once('error', reject);
                server.once('listening', () => resolve());
                server.listen(p, '127.0.0.1');
            });
            server.close();
            return p;
        } catch {
            server.close();
            // Try next port
        }
    }
    throw new Error(`No available port found starting from ${startPort}`);
}

type SessionStatus = 'running' | 'completed' | 'failed' | 'cancelled';

type DownloadSession = {
    id: string;
    status: SessionStatus;
    startedAt: string;
    endedAt?: string;
    exitCode?: number;
    logs: string[];
    process?: ChildProcessWithoutNullStreams;
    clients: ServerResponse[];
};

type CourseRecord = {
    id: number;
    fullname: string;
    display_name: string;
};

type CoursesResponse = {
    courses?: CourseRecord[];
    error?: string;
    stderr?: string;
};

type DownloadRequest = {
    site: string;
    username: string;
    password: string;
    force?: boolean;
    jobs?: number;
    allCourses?: boolean;
    courseIds?: string[];
    saveEnv?: boolean;
};

const sessions = new Map<string, DownloadSession>();

function parseJsonBody<T>(req: IncomingMessage): Promise<T> {
    return new Promise((resolve, reject) => {
        const chunks: Buffer[] = [];
        let totalBytes = 0;
        let rejected = false;
        req.on('data', (chunk) => {
            if (rejected) return;
            const buffer = Buffer.from(chunk);
            totalBytes += buffer.length;
            if (totalBytes > MAX_BODY_BYTES) {
                rejected = true;
                reject(new Error('Request body too large'));
                req.resume();
                return;
            }
            chunks.push(buffer);
        });
        req.on('end', () => {
            if (rejected) return;
            const raw = Buffer.concat(chunks).toString('utf8').trim();
            if (!raw) {
                resolve({} as T);
                return;
            }
            try {
                resolve(JSON.parse(raw) as T);
            } catch (error) {
                reject(error);
            }
        });
        req.on('error', reject);
    });
}

function validateSite(site: string): string | null {
    try {
        const parsed = new URL((site || '').trim());
        if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password) return null;
        return parsed.toString().replace(/\/$/, '');
    } catch {
        return null;
    }
}

function isLocalOrigin(req: IncomingMessage): boolean {
    const origin = req.headers.origin;
    if (!origin) return true;

    try {
        const parsed = new URL(origin);
        return parsed.protocol === 'http:' &&
            (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost') &&
            parsed.port === String(port);
    } catch {
        return false;
    }
}

function sendJson(res: ServerResponse, statusCode: number, body: unknown) {
    res.writeHead(statusCode, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(body));
}

function sendText(res: ServerResponse, statusCode: number, body: string, contentType = 'text/plain; charset=utf-8') {
    res.writeHead(statusCode, { 'Content-Type': contentType });
    res.end(body);
}

function writeEnvFile(site: string, username: string, password: string) {
    const envPath = path.join(repoRoot, '.env');
    const content = `MOODLE_SITE="${site}"
MOODLE_USERNAME="${username}"
MOODLE_PASSWORD="${password}"
`;
    return writeFile(envPath, content, 'utf8');
}

function appendSessionLog(session: DownloadSession, line: string) {
    session.logs.push(line);
    if (session.logs.length > 500) {
        session.logs.splice(0, session.logs.length - 500);
    }
    for (const client of session.clients) {
        client.write(`event: log\ndata: ${JSON.stringify({ line })}\n\n`);
    }
}

function broadcastStatus(session: DownloadSession) {
    for (const client of session.clients) {
        client.write(`event: status\ndata: ${JSON.stringify({ status: session.status })}\n\n`);
    }
}

function broadcastDone(session: DownloadSession) {
    for (const client of session.clients) {
        client.write(`event: done\ndata: ${JSON.stringify({ exitCode: session.exitCode ?? 0 })}\n\n`);
    }
}

function startSession(commandArgs: string[], env: NodeJS.ProcessEnv) {
    const sessionId = randomUUID();
    const session: DownloadSession = {
        id: sessionId,
        status: 'running',
        startedAt: new Date().toISOString(),
        logs: [],
        clients: [],
    };

    const child = spawn(pythonBinary, commandArgs, {
        cwd: repoRoot,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
    });

    session.process = child as unknown as ChildProcessWithoutNullStreams;
    sessions.set(sessionId, session);

    child.stdout.on('data', (chunk: Buffer) => appendSessionLog(session, chunk.toString('utf8')));
    child.stderr.on('data', (chunk: Buffer) => appendSessionLog(session, chunk.toString('utf8')));

    child.on('exit', (code, signal) => {
        session.exitCode = code ?? 0;
        session.endedAt = new Date().toISOString();
        if (session.status !== 'cancelled') {
            session.status = code === 0 ? 'completed' : 'failed';
        }
        appendSessionLog(session, `\nProceso finalizado (${signal ?? `code ${session.exitCode}`}).\n`);
        broadcastStatus(session);
        broadcastDone(session);
        for (const client of session.clients) {
            client.end();
        }
        session.clients = [];
    });

    return session;
}

async function listCourses(body: CoursesResponse & DownloadRequest) {
    const site = validateSite(body.site);
    if (!site) throw new Error('site debe ser una URL HTTPS válida sin credenciales');

    const env = {
        ...process.env,
        MOODLE_SITE: site,
        MOODLE_USERNAME: body.username,
        MOODLE_PASSWORD: body.password,
    };

    if (body.saveEnv) {
        await writeEnvFile(site, body.username, body.password);
    }

    return new Promise<CoursesResponse>((resolve) => {
        const child = spawn(pythonBinary, ['main.py', '--list-courses'], {
            cwd: repoRoot,
            env,
            stdio: ['ignore', 'pipe', 'pipe'],
            windowsHide: true,
        });

        let stdout = '';
        let stderr = '';

        child.stdout.on('data', (chunk: Buffer) => {
            stdout += chunk.toString('utf8');
        });
        child.stderr.on('data', (chunk: Buffer) => {
            stderr += chunk.toString('utf8');
        });

        child.on('exit', (code) => {
            if (code !== 0) {
                resolve({ error: `Python exited with code ${code}`, stderr });
                return;
            }

            try {
                resolve(JSON.parse(stdout) as CoursesResponse);
            } catch (error) {
                resolve({ error: 'Could not parse course list JSON', stderr: `${stderr}\n${stdout}` });
            }
        });
    });
}

async function handleRequest(req: IncomingMessage, res: ServerResponse) {
    const url = new URL(req.url || '/', `http://${req.headers.host || `127.0.0.1:${port}`}`);

    if (req.method === 'POST' && !isLocalOrigin(req)) {
        sendJson(res, 403, { error: 'Origen no permitido' });
        return;
    }

    if (req.method === 'GET' && url.pathname === '/') {
        const html = await readFile(publicIndexPath, 'utf8');
        sendText(res, 200, html, 'text/html; charset=utf-8');
        return;
    }

    if (req.method === 'POST' && url.pathname === '/api/courses') {
        try {
            const body = await parseJsonBody<DownloadRequest>(req);
            if (!body.site || !body.username || !body.password) {
                sendJson(res, 400, { error: 'site, username y password son obligatorios' });
                return;
            }

            if (!validateSite(body.site)) {
                sendJson(res, 400, { error: 'site debe ser una URL HTTPS válida sin credenciales' });
                return;
            }

            const coursesResponse = await listCourses(body);
            if (coursesResponse.error) {
                sendJson(res, 500, coursesResponse);
                return;
            }

            sendJson(res, 200, coursesResponse);
        } catch (error) {
            sendJson(res, 400, { error: 'JSON inválido', details: String(error) });
        }
        return;
    }

    if (req.method === 'POST' && url.pathname === '/api/download/start') {
        try {
            const body = await parseJsonBody<DownloadRequest>(req);
            if (!body.site || !body.username || !body.password) {
                sendJson(res, 400, { error: 'site, username y password son obligatorios' });
                return;
            }

            const site = validateSite(body.site);
            if (!site) {
                sendJson(res, 400, { error: 'site debe ser una URL HTTPS válida sin credenciales' });
                return;
            }

            if (body.saveEnv) {
                await writeEnvFile(site, body.username, body.password);
            }

            const env = {
                ...process.env,
                MOODLE_SITE: site,
                MOODLE_USERNAME: body.username,
                MOODLE_PASSWORD: body.password,
            };

            const commandArgs = ['main.py'];
            if (body.force) commandArgs.push('--force');
            if (typeof body.jobs === 'number' && Number.isFinite(body.jobs) && body.jobs > 0) {
                commandArgs.push('--jobs', String(Math.trunc(body.jobs)));
            }
            if (body.allCourses) {
                commandArgs.push('--all-courses');
            } else if (body.courseIds && body.courseIds.length > 0) {
                commandArgs.push('--courses', body.courseIds.join(','));
            }

            const activeSessions = [...sessions.values()].filter((candidate) => candidate.status === 'running').length;
            if (activeSessions >= MAX_ACTIVE_SESSIONS) {
                sendJson(res, 429, { error: 'Ya hay demasiadas descargas en ejecución' });
                return;
            }

            const session = startSession(commandArgs, env);
            sendJson(res, 200, { sessionId: session.id });
        } catch (error) {
            sendJson(res, 400, { error: 'JSON inválido', details: String(error) });
        }
        return;
    }

    if (req.method === 'GET' && url.pathname.startsWith('/api/sessions/')) {
        const match = url.pathname.match(/^\/api\/sessions\/([^/]+)(?:\/events|)$/);
        const sessionId = match?.[1];
        if (!sessionId) {
            sendJson(res, 404, { error: 'Sesión no encontrada' });
            return;
        }

        const session = sessions.get(sessionId);
        if (!session) {
            sendJson(res, 404, { error: 'Sesión no encontrada' });
            return;
        }

        if (url.pathname.endsWith('/events')) {
            res.writeHead(200, {
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform',
                Connection: 'keep-alive',
                'X-Accel-Buffering': 'no',
            });
            res.write(`event: status\ndata: ${JSON.stringify({ status: session.status })}\n\n`);
            if (session.logs.length) {
                for (const line of session.logs) {
                    res.write(`event: log\ndata: ${JSON.stringify({ line })}\n\n`);
                }
            }
            if (session.status !== 'running') {
                res.write(`event: done\ndata: ${JSON.stringify({ exitCode: session.exitCode ?? 0 })}\n\n`);
                res.end();
                return;
            }

            session.clients.push(res);
            req.on('close', () => {
                session.clients = session.clients.filter((client) => client !== res);
            });
            return;
        }

        sendJson(res, 200, {
            id: session.id,
            status: session.status,
            startedAt: session.startedAt,
            endedAt: session.endedAt,
            exitCode: session.exitCode,
            logs: session.logs,
        });
        return;
    }

    if (req.method === 'POST' && url.pathname.startsWith('/api/sessions/') && url.pathname.endsWith('/cancel')) {
        const sessionId = url.pathname.split('/')[3];
        const session = sessions.get(sessionId);
        if (!session || !session.process) {
            sendJson(res, 404, { error: 'Sesión no encontrada' });
            return;
        }

        session.status = 'cancelled';
        session.process.kill('SIGTERM');
        appendSessionLog(session, '\nProceso cancelado por el usuario.\n');
        broadcastStatus(session);
        sendJson(res, 200, { status: 'cancelled' });
        return;
    }

    sendJson(res, 404, { error: 'Ruta no encontrada' });
}

const server = createServer((req, res) => {
    void handleRequest(req, res);
});

findAvailablePort(port)
    .then((availablePort) => {
        port = availablePort;
        server.listen(port, '127.0.0.1', () => {
            console.log(`MooviDump web server running at http://127.0.0.1:${port}`);
        });
    })
    .catch((error) => {
        console.error('Failed to find available port:', error.message);
        process.exit(1);
    });
