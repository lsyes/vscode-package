/*---------------------------------------------------------------------------------------------
 *  vsda - LoongArch64 JavaScript shim
 *--------------------------------------------------------------------------------------------*/

export declare class signer {
	sign(arg: string): string;
}

export declare class validator {
	createNewMessage(arg: string): string;
	validate(arg: string): 'ok' | 'error';
	dispose?(): void;
}

declare const vsda: { signer: typeof signer; validator: typeof validator };
export default vsda;
