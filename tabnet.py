import numpy as np
import tensorflow as tf

from custom_objects import glu, sparsemax


class TransformBlock(tf.keras.Model):

    def __init__(self, features,
                 momentum,
                 virtual_batch_size=None,
                 **kwargs):
        super(TransformBlock, self).__init__(**kwargs)

        self.features = features
        self.momentum = momentum
        self.virtual_batch_size = virtual_batch_size

        self.transform = tf.keras.layers.Dense(self.features, use_bias=False)
        self.bn = tf.keras.layers.BatchNormalization(axis=-1, momentum=momentum,
                                                     virtual_batch_size=virtual_batch_size)

    def call(self, inputs, training=None):
        x = self.transform(inputs)
        x = self.bn(x, training=training)
        return x


class TabNet(tf.keras.Model):

    def __init__(self, feature_columns,
                 feature_dim=64,
                 num_features=None,
                 output_dim=64,
                 num_decision_steps=5,
                 relaxation_factor=1.5,
                 batch_momentum=0.98,
                 virtual_batch_size=None,
                 lambd_sparsity=1e-5,
                 **kwargs):
        """
        Tensorflow 2.0 implementation of [tf-TabNet: Attentive Interpretable Tabular Learning](https://arxiv.org/abs/1908.07442)

        # Hyper Parameter Tuning (Excerpt from the paper)
        We consider datasets ranging from ∼10K to ∼10M training points, with varying degrees of fitting
        difficulty. tf-TabNet obtains high performance for all with a few general principles on hyperparameter
        selection:

            - Most datasets yield the best results for Nsteps ∈ [3, 10]. Typically, larger datasets and
            more complex tasks require a larger Nsteps. A very high value of Nsteps may suffer from
            overfitting and yield poor generalization.

            - Adjustment of the values of Nd and Na is the most efficient way of obtaining a trade-off
            between performance and complexity. Nd = Na is a reasonable choice for most datasets. A
            very high value of Nd and Na may suffer from overfitting and yield poor generalization.

            - An optimal choice of γ can have a major role on the overall performance. Typically a larger
            Nsteps value favors for a larger γ.

            - A large batch size is beneficial for performance - if the memory constraints permit, as large
            as 1-10 % of the total training dataset size is suggested. The virtual batch size is typically
            much smaller than the batch size.

            - Initially large learning rate is important, which should be gradually decayed until convergence.

        Args:
            feature_columns:
            num_features:
            feature_dim:
            output_dim:
            num_decision_steps:
            relaxation_factor:
            batch_momentum:
            virtual_batch_size:
            lambd_sparsity:
            **kwargs:
        """
        super(TabNet, self).__init__(**kwargs)

        self.feature_columns = feature_columns
        self.num_features = num_features if num_features is not None else len(feature_columns)
        self.feature_dim = feature_dim
        self.output_dim = output_dim

        self.num_decision_steps = num_decision_steps
        self.relaxation_factor = relaxation_factor
        self.batch_momentum = batch_momentum
        self.virtual_batch_size = virtual_batch_size
        self.epsilon = lambd_sparsity

        self.input_features = tf.keras.layers.DenseFeatures(feature_columns)
        self.input_bn = tf.keras.layers.BatchNormalization(axis=-1, momentum=batch_momentum)

        self.transform_f1 = TransformBlock(2 * self.feature_dim, self.batch_momentum, self.virtual_batch_size)
        self.transform_f2 = TransformBlock(2 * self.feature_dim, self.batch_momentum, self.virtual_batch_size)
        self.transform_f3 = TransformBlock(2 * self.feature_dim, self.batch_momentum, self.virtual_batch_size)
        self.transform_f4 = TransformBlock(2 * self.feature_dim, self.batch_momentum, self.virtual_batch_size)
        self.transform_coef = TransformBlock(self.num_features, self.batch_momentum, self.virtual_batch_size)

    def call(self, inputs, training=None):
        features = self.input_features(inputs)
        features = self.input_bn(features, training=training)

        batch_size = tf.shape(features)[0]

        # Initializes decision-step dependent variables.
        output_aggregated = tf.zeros([batch_size, self.output_dim])
        masked_features = features
        mask_values = tf.zeros([batch_size, self.num_features])
        aggregated_mask_values = tf.zeros([batch_size, self.num_features])
        complemantary_aggregated_mask_values = tf.ones(
            [batch_size, self.num_features])

        total_entropy = 0.0
        for ni in tf.range(self.num_decision_steps):

            # Feature transformer with two shared and two decision step dependent
            # blocks is used below.
            transform_f1 = self.transform_f1(masked_features, training=training)
            transform_f1 = glu(transform_f1, self.feature_dim)

            transform_f2 = self.transform_f2(transform_f1, training=training)
            transform_f2 = (glu(transform_f2, self.feature_dim) +
                            transform_f1) * np.sqrt(0.5)

            transform_f3 = self.transform_f3(transform_f2, training=training)
            transform_f3 = (glu(transform_f3, self.feature_dim) +
                            transform_f2) * np.sqrt(0.5)

            transform_f4 = self.transform_f4(transform_f3, training=training)
            transform_f4 = (glu(transform_f4, self.feature_dim) +
                            transform_f3) * np.sqrt(0.5)

            if ni > 0:
                decision_out = tf.nn.relu(transform_f4[:, :self.output_dim])

                # Decision aggregation.
                output_aggregated += decision_out

                # Aggregated masks are used for visualization of the
                # feature importance attributes.
                scale_agg = tf.reduce_sum(decision_out, axis=1, keepdims=True)
                scale_agg = scale_agg / (self.num_decision_steps - 1)

                aggregated_mask_values += mask_values * scale_agg

            features_for_coef = (transform_f4[:, self.output_dim:])

            if (ni < self.num_decision_steps - 1):
                # Determines the feature masks via linear and nonlinear
                # transformations, taking into account of aggregated feature use.
                mask_values = self.transform_coef(features_for_coef, training=training)
                mask_values *= complemantary_aggregated_mask_values
                mask_values = sparsemax(mask_values, axis=-1)

                # Relaxation factor controls the amount of reuse of features between
                # different decision blocks and updated with the values of
                # coefficients.
                complemantary_aggregated_mask_values *= (
                        self.relaxation_factor - mask_values)

                # Entropy is used to penalize the amount of sparsity in feature
                # selection.
                total_entropy += tf.reduce_mean(
                    tf.reduce_sum(
                        -mask_values * tf.math.log(mask_values + self.epsilon), axis=1)) / (
                        tf.cast(self.num_decision_steps - 1, tf.float32))

                # Feature selection.
                masked_features = tf.multiply(mask_values, features)

                # # Visualization of the feature selection mask at decision step ni
                # tf.summary.image(
                #     "Mask for step" + str(ni),
                #     tf.expand_dims(tf.expand_dims(mask_values, 0), 3),
                #     max_outputs=1)

        # Visualization of the aggregated feature importances
        # tf.summary.image(
        #     "Aggregated mask",
        #     tf.expand_dims(tf.expand_dims(aggregated_mask_values, 0), 3),
        #     max_outputs=1)

        return output_aggregated, total_entropy


class TabNetClassification(tf.keras.Model):

    def __init__(self, feature_columns,
                 num_classes,
                 num_features=None,
                 feature_dim=64,
                 output_dim=64,
                 num_decision_steps=5,
                 relaxation_factor=1.5,
                 batch_momentum=0.98,
                 virtual_batch_size=None,
                 lambd_sparsity=1e-5,
                 **kwargs):
        super(TabNetClassification, self).__init__(**kwargs)

        self.num_classes = num_classes

        self.tabnet = TabNet(feature_columns=feature_columns,
                             num_features=num_features,
                             feature_dim=feature_dim,
                             output_dim=output_dim,
                             num_decision_steps=num_decision_steps,
                             relaxation_factor=relaxation_factor,
                             batch_momentum=batch_momentum,
                             virtual_batch_size=virtual_batch_size,
                             lambd_sparsity=lambd_sparsity,
                             **kwargs)

        self.clf = tf.keras.layers.Dense(num_classes, activation='softmax', use_bias=False)

    def call(self, inputs, training=None):
        self.activations, self.total_entropy = self.tabnet(inputs, training=training)
        out = self.clf(self.activations)

        return out


class TabNetRegression(tf.keras.Model):

    def __init__(self, feature_columns,
                 num_regressors,
                 num_features=None,
                 feature_dim=64,
                 output_dim=64,
                 num_decision_steps=5,
                 relaxation_factor=1.5,
                 batch_momentum=0.98,
                 virtual_batch_size=None,
                 lambd_sparsity=1e-5,
                 **kwargs):
        super(TabNetRegression, self).__init__(**kwargs)

        self.num_regressors = num_regressors

        self.tabnet = TabNet(feature_columns=feature_columns,
                             num_features=num_features,
                             feature_dim=feature_dim,
                             output_dim=output_dim,
                             num_decision_steps=num_decision_steps,
                             relaxation_factor=relaxation_factor,
                             batch_momentum=batch_momentum,
                             virtual_batch_size=virtual_batch_size,
                             lambd_sparsity=lambd_sparsity,
                             **kwargs)

        self.regressor = tf.keras.layers.Dense(num_regressors, use_bias=False)

    def call(self, inputs, training=None):
        self.activations, self.total_entropy = self.tabnet(inputs, training=training)
        out = self.regressor(self.activations)
        return out
