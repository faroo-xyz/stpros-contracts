export function readOnlyRpc(url: string, simulation?: boolean, localOnly?: boolean): (method: string, params: any[]) => Promise<any>;
export function recheckObserved(c: any, inventory: any, rpc: any, options?: any): Promise<any>;
export function estimateGas(c: any, transactions: any[], actor: string, rpc: any): Promise<any>;
export function verifyCheckpoint(c: any, inventory: any, artifacts: any, rpc: any, stage?: string, options?: any): Promise<any>;
