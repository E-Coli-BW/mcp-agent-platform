package com.example.agent.governance;

/**
 * Emits model-call provenance events to an external sink.
 */
public interface ProvenanceEmitter {

    void emitModelCall(ModelCallProvenance event);
}

