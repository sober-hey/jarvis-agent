// 粒子形态转换系统 - 核心引擎
// 基于GPU加速的粒子系统

class ParticleMorphEngine {
    constructor() {
        this.canvas = document.getElementById('canvas');
        this.scene = null;
        this.camera = null;
        this.renderer = null;
        this.particles = null;
        this.particleCount = 50000;
        this.currentState = 'free';
        this.transitionProgress = 0;
        this.isTransitioning = false;

        // 粒子数据
        this.positions = null;
        this.targetPositions = null;
        this.velocities = null;
        this.colors = null;
        this.sizes = null;

        // 鼠标控制
        this.mouse = { x: 0, y: 0 };
        this.isDragging = false;
        this.previousMouse = { x: 0, y: 0 };
        this.rotation = { x: 0, y: 0 };
        this.zoom = 15;

        // 时间
        this.time = 0;
        this.clock = new THREE.Clock();

        this.init();
    }

    init() {
        this.setupScene();
        this.createParticleSystem();
        this.setupControls();
        this.setupEventListeners();
        this.animate();

        // 初始化为自由态
        this.setState('free');
        this.showStatus('系统已启动 - 自由态');
    }

    setupScene() {
        // 场景
        this.scene = new THREE.Scene();
        this.scene.fog = new THREE.Fog(0x000000, 10, 50);

        // 相机
        this.camera = new THREE.PerspectiveCamera(
            60,
            window.innerWidth / window.innerHeight,
            0.1,
            1000
        );
        this.camera.position.z = this.zoom;

        // 渲染器
        this.renderer = new THREE.WebGLRenderer({
            canvas: this.canvas,
            antialias: true,
            alpha: true
        });
        this.renderer.setSize(window.innerWidth, window.innerHeight);
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        this.renderer.setClearColor(0x000000);

        // 添加星空背景
        this.createStarfield();
    }

    createStarfield() {
        const starsGeometry = new THREE.BufferGeometry();
        const starPositions = [];

        for (let i = 0; i < 5000; i++) {
            const x = (Math.random() - 0.5) * 100;
            const y = (Math.random() - 0.5) * 100;
            const z = (Math.random() - 0.5) * 100;
            starPositions.push(x, y, z);
        }

        starsGeometry.setAttribute('position',
            new THREE.Float32BufferAttribute(starPositions, 3));

        const starsMaterial = new THREE.PointsMaterial({
            color: 0xffffff,
            size: 0.05,
            transparent: true,
            opacity: 0.6
        });

        const stars = new THREE.Points(starsGeometry, starsMaterial);
        this.scene.add(stars);
    }

    createParticleSystem() {
        const geometry = new THREE.BufferGeometry();

        // 初始化粒子数据数组
        this.positions = new Float32Array(this.particleCount * 3);
        this.targetPositions = new Float32Array(this.particleCount * 3);
        this.velocities = new Float32Array(this.particleCount * 3);
        this.colors = new Float32Array(this.particleCount * 3);
        this.sizes = new Float32Array(this.particleCount);

        // 初始化粒子为随机位置
        for (let i = 0; i < this.particleCount; i++) {
            const i3 = i * 3;

            // 初始位置：球形分布
            const radius = 5;
            const theta = Math.random() * Math.PI * 2;
            const phi = Math.acos(2 * Math.random() - 1);

            this.positions[i3] = radius * Math.sin(phi) * Math.cos(theta);
            this.positions[i3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
            this.positions[i3 + 2] = radius * Math.cos(phi);

            // 初始速度
            this.velocities[i3] = (Math.random() - 0.5) * 0.02;
            this.velocities[i3 + 1] = (Math.random() - 0.5) * 0.02;
            this.velocities[i3 + 2] = (Math.random() - 0.5) * 0.02;

            // 颜色：蓝色和粉色混合
            if (Math.random() > 0.5) {
                this.colors[i3] = 0.0;      // R
                this.colors[i3 + 1] = 0.75; // G
                this.colors[i3 + 2] = 1.0;  // B (蓝色)
            } else {
                this.colors[i3] = 1.0;      // R
                this.colors[i3 + 1] = 0.4;  // G
                this.colors[i3 + 2] = 0.8;  // B (粉色)
            }

            // 大小
            this.sizes[i] = Math.random() * 2 + 1;
        }

        geometry.setAttribute('position',
            new THREE.BufferAttribute(this.positions, 3));
        geometry.setAttribute('color',
            new THREE.BufferAttribute(this.colors, 3));
        geometry.setAttribute('size',
            new THREE.BufferAttribute(this.sizes, 1));

        // 粒子材质
        const material = new THREE.PointsMaterial({
            size: 0.05,
            vertexColors: true,
            transparent: true,
            opacity: 0.8,
            blending: THREE.AdditiveBlending,
            depthWrite: false
        });

        this.particles = new THREE.Points(geometry, material);
        this.scene.add(this.particles);
    }

    // 自由态粒子运动
    updateFreeState(deltaTime) {
        for (let i = 0; i < this.particleCount; i++) {
            const i3 = i * 3;

            // Lissajous曲线 + 布朗运动
            const freq = 0.5;
            const t = this.time + i * 0.001;

            this.velocities[i3] += Math.sin(t * freq) * 0.0001;
            this.velocities[i3 + 1] += Math.cos(t * freq * 1.3) * 0.0001;
            this.velocities[i3 + 2] += Math.sin(t * freq * 0.7) * 0.0001;

            // 添加随机扰动
            this.velocities[i3] += (Math.random() - 0.5) * 0.0005;
            this.velocities[i3 + 1] += (Math.random() - 0.5) * 0.0005;
            this.velocities[i3 + 2] += (Math.random() - 0.5) * 0.0005;

            // 应用速度
            this.positions[i3] += this.velocities[i3];
            this.positions[i3 + 1] += this.velocities[i3 + 1];
            this.positions[i3 + 2] += this.velocities[i3 + 2];

            // 速度衰减
            this.velocities[i3] *= 0.98;
            this.velocities[i3 + 1] *= 0.98;
            this.velocities[i3 + 2] *= 0.98;

            // 边界约束：保持在球形范围内
            const x = this.positions[i3];
            const y = this.positions[i3 + 1];
            const z = this.positions[i3 + 2];
            const dist = Math.sqrt(x * x + y * y + z * z);

            if (dist > 8) {
                const factor = 8 / dist;
                this.positions[i3] *= factor;
                this.positions[i3 + 1] *= factor;
                this.positions[i3 + 2] *= factor;
            }
        }

        this.particles.geometry.attributes.position.needsUpdate = true;
    }

    // 生成火箭发动机结构的目标位置
    generateRocketEngineTargets() {
        const targets = [];

        // 1. 燃烧室 (主体圆柱)
        const chamberSamples = Math.floor(this.particleCount * 0.3);
        for (let i = 0; i < chamberSamples; i++) {
            const theta = Math.random() * Math.PI * 2;
            const y = (Math.random() - 0.5) * 2;
            const radius = 0.8 + Math.random() * 0.2;

            targets.push({
                x: Math.cos(theta) * radius,
                y: y,
                z: Math.sin(theta) * radius,
                part: 'chamber'
            });
        }

        // 2. 喷嘴 (锥形)
        const nozzleSamples = Math.floor(this.particleCount * 0.25);
        for (let i = 0; i < nozzleSamples; i++) {
            const theta = Math.random() * Math.PI * 2;
            const t = Math.random();
            const y = -1 - t * 1.5;
            const radius = 0.5 + t * 0.7;

            targets.push({
                x: Math.cos(theta) * radius,
                y: y,
                z: Math.sin(theta) * radius,
                part: 'nozzle'
            });
        }

        // 3. 涡轮泵 (侧面盒子)
        const pumpSamples = Math.floor(this.particleCount * 0.15);
        for (let i = 0; i < pumpSamples; i++) {
            targets.push({
                x: 1.2 + (Math.random() - 0.5) * 0.8,
                y: 0.5 + (Math.random() - 0.5) * 0.8,
                z: (Math.random() - 0.5) * 0.8,
                part: 'pump'
            });
        }

        // 4. 预燃室 (球体)
        const preburnerSamples = Math.floor(this.particleCount * 0.1);
        for (let i = 0; i < preburnerSamples; i++) {
            const theta = Math.random() * Math.PI * 2;
            const phi = Math.acos(2 * Math.random() - 1);
            const radius = 0.4;

            targets.push({
                x: -1 + radius * Math.sin(phi) * Math.cos(theta),
                y: 1 + radius * Math.sin(phi) * Math.sin(theta),
                z: radius * Math.cos(phi),
                part: 'preburner'
            });
        }

        // 5. 管道连接线
        const pipeSamples = Math.floor(this.particleCount * 0.2);
        for (let i = 0; i < pipeSamples; i++) {
            const t = i / pipeSamples;
            targets.push({
                x: 0.8 * t,
                y: 0.5,
                z: (Math.random() - 0.5) * 0.2,
                part: 'pipe'
            });
        }

        return targets;
    }

    // 生成拆解状态的目标位置
    generateExplodedTargets() {
        const targets = [];
        const rocketTargets = this.generateRocketEngineTargets();

        // 将每个部件沿径向推开
        rocketTargets.forEach((target, index) => {
            const direction = new THREE.Vector3(
                target.x,
                target.y,
                target.z
            ).normalize();

            // 不同部件推开不同距离
            let pushDistance = 2;
            if (target.part === 'nozzle') pushDistance = 2.5;
            if (target.part === 'pump') pushDistance = 1.5;
            if (target.part === 'preburner') pushDistance = 1.8;

            targets.push({
                x: target.x + direction.x * pushDistance,
                y: target.y + direction.y * pushDistance,
                z: target.z + direction.z * pushDistance,
                part: target.part
            });
        });

        return targets;
    }

    // 状态切换
    async setState(state) {
        if (this.isTransitioning) return;

        this.currentState = state;
        this.isTransitioning = true;
        this.transitionProgress = 0;

        // 根据状态生成目标位置
        let targets = [];
        let duration = 2000; // 2秒

        switch (state) {
            case 'free':
                // 回到自由态 - 随机分布
                for (let i = 0; i < this.particleCount; i++) {
                    const radius = 5 + Math.random() * 3;
                    const theta = Math.random() * Math.PI * 2;
                    const phi = Math.acos(2 * Math.random() - 1);

                    targets.push({
                        x: radius * Math.sin(phi) * Math.cos(theta),
                        y: radius * Math.sin(phi) * Math.sin(theta),
                        z: radius * Math.cos(phi),
                        part: 'free'
                    });
                }
                this.showStatus('自由态');
                break;

            case 'rocket':
                targets = this.generateRocketEngineTargets();
                this.showStatus('形态转换中 → 火箭发动机');
                duration = 3000; // 3秒，慢一点更有仪式感
                break;

            case 'explode':
                targets = this.generateExplodedTargets();
                this.showStatus('拆解中...');
                duration = 2500;
                break;

            case 'ignite':
                // 点火不改变位置，只添加特效
                this.createIgnitionEffect();
                this.showStatus('点火！');
                this.isTransitioning = false;
                return;
        }

        // 分配目标位置
        this.assignTargets(targets);

        // 开始转换动画
        const startTime = Date.now();
        const animate = () => {
            const elapsed = Date.now() - startTime;
            this.transitionProgress = Math.min(elapsed / duration, 1);

            if (this.transitionProgress < 1) {
                requestAnimationFrame(animate);
            } else {
                this.isTransitioning = false;
                this.showStatus('转换完成', 1500);
            }
        };

        animate();
    }

    // 分配粒子到目标位置（最近邻+随机扰动）
    assignTargets(targets) {
        // 简单策略：按顺序分配，添加小延迟
        for (let i = 0; i < this.particleCount; i++) {
            const targetIndex = Math.floor((i / this.particleCount) * targets.length);
            const target = targets[Math.min(targetIndex, targets.length - 1)];

            this.targetPositions[i * 3] = target.x;
            this.targetPositions[i * 3 + 1] = target.y;
            this.targetPositions[i * 3 + 2] = target.z;
        }
    }

    // 更新转换动画
    updateTransition() {
        if (!this.isTransitioning) return;

        const eased = this.easeInOutCubic(this.transitionProgress);

        for (let i = 0; i < this.particleCount; i++) {
            const i3 = i * 3;

            // 当前位置插值到目标位置
            const startX = this.positions[i3];
            const startY = this.positions[i3 + 1];
            const startZ = this.positions[i3 + 2];

            const targetX = this.targetPositions[i3];
            const targetY = this.targetPositions[i3 + 1];
            const targetZ = this.targetPositions[i3 + 2];

            this.positions[i3] = startX + (targetX - startX) * eased;
            this.positions[i3 + 1] = startY + (targetY - startY) * eased;
            this.positions[i3 + 2] = startZ + (targetZ - startZ) * eased;

            // 添加微小扰动
            if (eased < 0.9) {
                this.positions[i3] += (Math.random() - 0.5) * 0.02 * (1 - eased);
                this.positions[i3 + 1] += (Math.random() - 0.5) * 0.02 * (1 - eased);
                this.positions[i3 + 2] += (Math.random() - 0.5) * 0.02 * (1 - eased);
            }
        }

        this.particles.geometry.attributes.position.needsUpdate = true;
    }

    // 缓动函数
    easeInOutCubic(t) {
        return t < 0.5
            ? 4 * t * t * t
            : 1 - Math.pow(-2 * t + 2, 3) / 2;
    }

    // 创建点火特效
    createIgnitionEffect() {
        // 找到喷嘴部分的粒子
        const ignitionParticles = [];

        for (let i = 0; i < this.particleCount; i++) {
            const i3 = i * 3;
            const y = this.positions[i3 + 1];

            // 喷嘴底部的粒子
            if (y < -2 && y > -3) {
                ignitionParticles.push(i);
            }
        }

        // 创建火焰效果
        const flameLife = 180; // 3秒 @ 60fps
        let frame = 0;

        const animateFlame = () => {
            frame++;

            ignitionParticles.forEach(i => {
                const i3 = i * 3;

                // 粒子向下喷射
                this.positions[i3 + 1] -= 0.05;

                // 添加扩散
                this.positions[i3] += (Math.random() - 0.5) * 0.03;
                this.positions[i3 + 2] += (Math.random() - 0.5) * 0.03;

                // 颜色变为橙黄色
                this.colors[i3] = 1.0;      // R
                this.colors[i3 + 1] = 0.5 + Math.random() * 0.3; // G
                this.colors[i3 + 2] = 0.1;  // B

                // 重置过低的粒子
                if (this.positions[i3 + 1] < -5) {
                    this.positions[i3 + 1] = -2.5;
                }
            });

            this.particles.geometry.attributes.position.needsUpdate = true;
            this.particles.geometry.attributes.color.needsUpdate = true;

            if (frame < flameLife) {
                requestAnimationFrame(animateFlame);
            } else {
                // 恢复颜色
                ignitionParticles.forEach(i => {
                    const i3 = i * 3;
                    if (Math.random() > 0.5) {
                        this.colors[i3] = 0.0;
                        this.colors[i3 + 1] = 0.75;
                        this.colors[i3 + 2] = 1.0;
                    } else {
                        this.colors[i3] = 1.0;
                        this.colors[i3 + 1] = 0.4;
                        this.colors[i3 + 2] = 0.8;
                    }
                });
                this.particles.geometry.attributes.color.needsUpdate = true;
            }
        };

        animateFlame();
    }

    // 鼠标和键盘控制
    setupControls() {
        window.addEventListener('mousedown', this.onMouseDown.bind(this));
        window.addEventListener('mousemove', this.onMouseMove.bind(this));
        window.addEventListener('mouseup', this.onMouseUp.bind(this));
        window.addEventListener('wheel', this.onWheel.bind(this));
        window.addEventListener('resize', this.onResize.bind(this));
    }

    onMouseDown(e) {
        this.isDragging = true;
        this.previousMouse.x = e.clientX;
        this.previousMouse.y = e.clientY;
    }

    onMouseMove(e) {
        if (!this.isDragging) return;

        const deltaX = e.clientX - this.previousMouse.x;
        const deltaY = e.clientY - this.previousMouse.y;

        this.rotation.y += deltaX * 0.005;
        this.rotation.x += deltaY * 0.005;

        // 限制X轴旋转
        this.rotation.x = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, this.rotation.x));

        this.previousMouse.x = e.clientX;
        this.previousMouse.y = e.clientY;
    }

    onMouseUp() {
        this.isDragging = false;
    }

    onWheel(e) {
        e.preventDefault();
        this.zoom += e.deltaY * 0.01;
        this.zoom = Math.max(5, Math.min(30, this.zoom));
    }

    onResize() {
        this.camera.aspect = window.innerWidth / window.innerHeight;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(window.innerWidth, window.innerHeight);
    }

    setupEventListeners() {
        // 按钮事件
        document.querySelectorAll('.btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const state = btn.dataset.state;
                if (state) {
                    this.setState(state);

                    // 更新按钮状态
                    document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                }
            });
        });
    }

    // 主动画循环
    animate() {
        requestAnimationFrame(this.animate.bind(this));

        const deltaTime = this.clock.getDelta();
        this.time += deltaTime;

        // 应用旋转
        if (this.particles) {
            this.particles.rotation.x = this.rotation.x;
            this.particles.rotation.y = this.rotation.y;
        }

        // 更新相机
        this.camera.position.z = this.zoom;

        // 根据状态更新粒子
        if (this.isTransitioning) {
            this.updateTransition();
        } else if (this.currentState === 'free') {
            this.updateFreeState(deltaTime);
        }

        // 渲染
        this.renderer.render(this.scene, this.camera);
    }

    // UI辅助
    showStatus(message, duration = 2000) {
        const status = document.getElementById('status');
        status.textContent = message;
        status.classList.add('show');

        setTimeout(() => {
            status.classList.remove('show');
        }, duration);
    }
}

// 初始化
window.addEventListener('DOMContentLoaded', () => {
    const engine = new ParticleMorphEngine();
    window.particleEngine = engine;
    console.log('粒子形态转换系统已启动');
});
