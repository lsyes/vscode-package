/*
 * LoongArch64 replacement for the closed-source `vsda` native module.
 *
 * VSCode uses `vsda` to add a tamper-evident handshake to remote connections:
 *
 *   const { default: vsda } = await import('vsda');
 *   const { data } = await signService.createNewMessage(value);  // validator side
 *   const signed  = await signService.sign(data);                // peer side
 *   const ok      = await signService.validate(message, signed);  // validator side
 *
 * which maps onto
 *
 *   new vsda.validator().createNewMessage(value) -> challenge
 *   new vsda.signer().sign(challenge)            -> signature
 *   new vsda.validator().validate(signature)     -> 'ok' | 'error'
 *
 * `AbstractSignService` degrades gracefully whenever the module cannot be
 * loaded, but using a real (symmetric) implementation keeps the handshake
 * property instead of silently disabling it.
 */

'use strict';

const crypto = require('crypto');

const DEFAULT_KEY = 'vscode-loong64-vsda-shim';
const NONCE_BYTES = 32;
/** how many outstanding challenges a validator remembers */
const MAX_PENDING = 64;

function key() {
	return process.env.VSCODE_LOONG64_SIGN_KEY || DEFAULT_KEY;
}

function mac(payload) {
	return crypto.createHmac('sha256', key()).update(payload, 'utf8').digest('hex');
}

function constantTimeEquals(left, right) {
	const a = Buffer.from(String(left), 'utf8');
	const b = Buffer.from(String(right), 'utf8');
	if (a.length !== b.length) {
		return false;
	}
	return crypto.timingSafeEqual(a, b);
}

/**
 * Signer: HMAC-SHA256 over the challenge with the process-wide key.
 */
class signer {
	// eslint-disable-next-line local/code-class-name-pascal-case
	sign(value) {
		const payload = typeof value === 'string' ? value : String(value);
		return mac(payload);
	}
}

/**
 * Validator: issues a random challenge and accepts only the HMAC computed by a
 * peer that holds the same key.
 */
class validator {
	constructor() {
		/** @type {string[]} */
		this._pending = [];
	}

	createNewMessage(value) {
		const nonce = crypto.randomBytes(NONCE_BYTES).toString('hex');
		this._pending.push(nonce);
		while (this._pending.length > MAX_PENDING) {
			this._pending.shift();
		}
		return nonce;
	}

	validate(value) {
		try {
			const provided = typeof value === 'string' ? value : String(value);
			if (!provided) {
				return 'error';
			}
			// Accept both the bare signature and the `<challenge>.<signature>`
			// form so a peer that echoes the challenge keeps working.
			const separator = provided.lastIndexOf('.');
			const challenge = separator > 0 ? provided.slice(0, separator) : undefined;
			const signature = separator > 0 ? provided.slice(separator + 1) : provided;
			let candidates;
			if (challenge !== undefined) {
				// an explicit challenge must still be outstanding
				if (!this._pending.includes(challenge)) {
					return 'error';
				}
				candidates = [challenge];
			} else {
				candidates = this._pending.slice().reverse();
			}
			for (const nonce of candidates) {
				if (constantTimeEquals(mac(nonce), signature)) {
					const index = this._pending.indexOf(nonce);
					if (index >= 0) {
						this._pending.splice(index, 1);
					}
					return 'ok';
				}
			}
			return 'error';
		} catch {
			return 'error';
		}
	}

	dispose() {
		this._pending.length = 0;
	}
}

module.exports = { signer, validator };
