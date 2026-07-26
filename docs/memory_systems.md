# Memory Systems Module Documentation

## Overview

The `memory_systems` module is a core component of the Kronos system responsible for managing and persisting entity and alias memories. It provides persistent storage and retrieval mechanisms for entities, their relationships, and aliases, enabling the system to learn and recall information across sessions.

This module is designed to work with multiple storage backends, including local JSON files and Neo4j for entity relationships, ensuring data persistence and efficient querying.

## Architecture Overview

The memory systems module follows a layered architecture with clear separation of concerns:

```mermaid
diagram TD
    A[API Layer] --> B[Core Memory Components]
    B --> C[Persistence Layer]
    B --> D[Neo4j Integration]
    C --> E[Local JSON Storage]
    D --> F[Neo4j Database]
```

### Core Components

The module consists of several key components organized into functional areas:

1. **Memory Core Components**
   - `AliasMemory`: Manages entity aliases and their canonical forms
   - `EntityMemory`: Stores and retrieves entity information with embeddings

2. **Integration Components**
   - Database clients for Neo4j and Qdrant
   - Configuration management

3. **Utility Components**
   - Atomic file operations
   - Quota tracking
   - Retry mechanisms

## Module Components

### Core Memory Components

#### AliasMemory (`agents/alias_memory.py`)

The `AliasMemory` class provides persistent storage for entity aliases, allowing the system to learn and recall alternative names for entities. It implements a confidence-based system where aliases are only committed to permanent memory if they meet a high confidence threshold.

**Key Features:**
- Persistent alias storage in JSON format
- Confidence-based alias commitment (threshold: 0.93)
- Reinforcement learning through repeated usage
- Conflict resolution for conflicting aliases
- Manual correction capability

**Entry Structure:**
```json
{
  "canonical": "Graphics Processing Unit",
  "confidence": 0.99,
  "times_seen": 72,
  "source": "embedding"
}
```

For detailed documentation, see [memory_systems_alias_memory.md](memory_systems_alias_memory.md).

#### EntityMemory (`agents/entity_memory.py`)

The `EntityMemory` class manages entity information including canonical names, types, descriptions, aliases, and relationships. It supports both local JSON storage and Neo4j for relationship data.

**Key Features:**
- Persistent entity storage with embeddings
- Entity similarity search using cosine similarity
- Relationship tracking between entities
- Dual storage backend support (JSON and Neo4j)
- Automatic hydration from Neo4j when local storage is empty

**Entry Structure:**
```json
{
  "canonical": "NVIDIA",
  "type": "company",
  "domain": "technology",
  "aliases": ["NVDA", "nVidia"],
  "descriptions": ["NVIDIA Corporation is an American multinational technology company..."],
  "occurrence_count": 42,
  "confidence": 0.95,
  "relationship_count": 8
}
```

For detailed documentation, see [memory_systems_entity_memory.md](memory_systems_entity_memory.md).

### Integration Components

#### Database Clients

The module integrates with two database systems:

1. **Neo4j Client** (`db/neo4j_client.py`):
   - Manages entity relationships
   - Provides graph-based querying capabilities
   - Used for hydrating entity memory when local storage is empty

2. **Qdrant Client** (`db/qdrant_client.py`):
   - Vector database for entity embeddings
   - Enables similarity search across entities
   - Integrated with EntityMemory for finding similar entities

#### Configuration

The `Config` class (`core/config.py`) provides centralized configuration management for the memory systems module, allowing customization of:
- Memory file paths
- Confidence thresholds
- Storage backends
- Performance parameters

### Utility Components

#### Atomic JSON Operations

The `atomic_write_json` utility ensures safe file operations by writing to temporary files and then atomically renaming them to the target location.

#### Quota Tracking

The `QuotaTracker` (`utils/quota.py`) manages API quotas and rate limits, preventing system overload and ensuring fair usage.

#### Retry Mechanisms

The `DailyQuotaExceeded` exception (`utils/retry.py`) handles rate limiting scenarios, allowing the system to gracefully handle API quota exhaustion.

## Data Flow

The memory systems module supports several key data flows:

### Entity Processing Flow

```mermaid
diagram TD
    A[New Entity Data] --> B[EntityMemory.upsert()]
    B --> C[Update Local JSON]
    B --> D[Update Neo4j Relationships]
    B --> E[Update Qdrant Embeddings]
    F[Similarity Search] --> G[EntityMemory.find_similar()]
    G --> H[Qdrant Vector Search]
```

### Alias Resolution Flow

```mermaid
diagram TD
    A[Entity Reference] --> B[AliasMemory.get()]
    B --> C{Found in AliasMemory?}
    C -->|Yes| D[Return Canonical Name]
    C -->|No| E[Use Original Name]
    D --> F[Reinforce Alias Usage]
    E --> G[Record New Alias if Confident]
```

## Integration with Other Modules

The memory systems module integrates with several other components in the Kronos system:

1. **Agent Framework**: Agents use memory systems to store and retrieve entity information
2. **Ontology Management**: Entity types and relationships are managed through the ontology system
3. **Data Processing**: Processed data is stored in memory systems for future reference
4. **Learning System**: Learned patterns and relationships are persisted in memory systems
5. **API Layer**: Provides endpoints for querying and updating memory

## Configuration

The memory systems module can be configured through the central `Config` class. Key configuration parameters include:

- `memory_file`: Path to the entity memory JSON file
- `alias_memory_file`: Path to the alias memory JSON file
- `neo4j_connection`: Neo4j database connection parameters
- `qdrant_connection`: Qdrant vector database connection parameters
- `confidence_thresholds`: Various confidence thresholds for memory operations

## Performance Considerations

1. **Memory Usage**: Entity and alias memories are stored in memory for fast access, with periodic flushing to disk.
2. **Embedding Storage**: Entity embeddings are stored in Qdrant for efficient similarity search.
3. **Relationship Queries**: Complex relationship queries are delegated to Neo4j for optimal performance.
4. **Atomic Operations**: All file operations are atomic to prevent data corruption.

## Error Handling

The module implements several error handling strategies:

1. **Graceful Degradation**: If Neo4j is unavailable, entity memory falls back to local JSON storage
2. **Conflict Resolution**: Conflicting aliases are resolved based on confidence scores
3. **Quota Management**: API quotas are tracked and respected
4. **Data Validation**: Input data is validated before storage

## Future Enhancements

1. **Distributed Caching**: Implement distributed caching for large-scale deployments
2. **Memory Compression**: Add compression for memory files to reduce storage requirements
3. **Incremental Updates**: Support for incremental updates to reduce I/O operations
4. **Memory Analytics**: Add analytics capabilities to track memory usage patterns
5. **Versioning**: Implement versioning for memory entries to support rollback

## See Also

- [memory_systems_alias_memory.md](memory_systems_alias_memory.md) - Detailed documentation for AliasMemory
- [memory_systems_entity_memory.md](memory_systems_entity_memory.md) - Detailed documentation for EntityMemory
- [agent_framework.md](agent_framework.md) - Documentation for the agent framework that uses memory systems
- [ontology_management.md](ontology_management.md) - Documentation for ontology management
- [database_clients.md](database_clients.md) - Documentation for database integration components